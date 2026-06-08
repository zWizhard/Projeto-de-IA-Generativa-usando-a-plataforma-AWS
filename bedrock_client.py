"""
bedrock_client.py
=================
Cliente do Amazon Bedrock responsável por interpretar a INTENÇÃO da pergunta do
usuário e devolver um JSON controlado. O modelo nunca executa SQL nem consulta o
banco — ele apenas classifica a pergunta.

Autenticação: reutiliza o fluxo de IAM Identity Center (SSO) já validado no
ambiente do curso. As credenciais temporárias são guardadas em cache local e
renovadas automaticamente quando expiram. Também aceita chaves estáticas via
variáveis de ambiente, para ambientes não interativos.

Região: o bedrock-runtime usa AWS_REGION (padrão us-east-2). O login SSO usa
SSO_REGION (us-east-1), pois é onde o Identity Center está hospedado.
"""
from __future__ import annotations

import json
import os
import re
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

import config
import prompts


# ---------------------------------------------------------------------------
# Exceções de domínio (mensagens amigáveis para a interface)
# ---------------------------------------------------------------------------
class BedrockError(Exception):
    """Erro de alto nível relacionado ao Amazon Bedrock."""


class BedrockAuthError(BedrockError):
    """Falha de autenticação/credenciais AWS."""


class BedrockPermissionError(BedrockError):
    """Sem permissão para invocar o modelo (AccessDenied)."""


class BedrockModelError(BedrockError):
    """Modelo indisponível, inexistente ou com throttling."""


# ---------------------------------------------------------------------------
# Cache de credenciais SSO
# ---------------------------------------------------------------------------
def _save_creds(creds: Dict[str, Any]) -> None:
    with open(config.BEDROCK_CREDS_CACHE, "w") as f:
        json.dump(creds, f)
    try:
        os.chmod(config.BEDROCK_CREDS_CACHE, 0o600)
    except OSError:
        # No Windows o chmod tem efeito limitado; ignorar é seguro.
        pass


def _load_creds() -> Optional[Dict[str, Any]]:
    if not os.path.exists(config.BEDROCK_CREDS_CACHE):
        return None
    try:
        with open(config.BEDROCK_CREDS_CACHE) as f:
            creds = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    # "expiration" vem em milissegundos (epoch) no formato do SSO.
    if creds.get("expiration", 0) < int(datetime.now(timezone.utc).timestamp() * 1000):
        return None
    return creds


def _sso_login() -> Dict[str, Any]:
    """Fluxo device-authorization do IAM Identity Center (abre o navegador)."""
    sso_oidc = boto3.client("sso-oidc", region_name=config.SSO_REGION)
    client = sso_oidc.register_client(clientName="chatbot-inep", clientType="public")
    auth = sso_oidc.start_device_authorization(
        clientId=client["clientId"],
        clientSecret=client["clientSecret"],
        startUrl=config.SSO_START_URL,
    )
    print(
        "\n🔐 Abrindo o navegador para login no AWS Identity Center...\n"
        f"   Se não abrir, acesse: {auth['verificationUriComplete']}\n"
    )
    try:
        webbrowser.open(auth["verificationUriComplete"])
    except Exception:
        pass

    token = None
    while not token:
        time.sleep(auth["interval"])
        try:
            token = sso_oidc.create_token(
                clientId=client["clientId"],
                clientSecret=client["clientSecret"],
                grantType="urn:ietf:params:oauth:grant-type:device_code",
                deviceCode=auth["deviceCode"],
            )
        except sso_oidc.exceptions.AuthorizationPendingException:
            pass
        except ClientError as exc:  # expired/slow_down/etc.
            raise BedrockAuthError(f"Falha no login SSO: {exc}") from exc

    sso = boto3.client("sso", region_name=config.SSO_REGION)
    try:
        creds = sso.get_role_credentials(
            roleName=config.SSO_ROLE_NAME,
            accountId=config.SSO_ACCOUNT_ID,
            accessToken=token["accessToken"],
        )["roleCredentials"]
    except ClientError as exc:
        raise BedrockAuthError(
            "Não foi possível obter credenciais do papel "
            f"'{config.SSO_ROLE_NAME}'. Verifique se você tem acesso ao grupo. "
            f"Detalhe: {exc}"
        ) from exc
    _save_creds(creds)
    return creds


def _resolve_credentials() -> Dict[str, Optional[str]]:
    """Resolve credenciais na ordem: estáticas no .env -> cache SSO -> login SSO."""
    if config.AWS_ACCESS_KEY_ID and config.AWS_SECRET_ACCESS_KEY:
        return {
            "accessKeyId": config.AWS_ACCESS_KEY_ID,
            "secretAccessKey": config.AWS_SECRET_ACCESS_KEY,
            "sessionToken": config.AWS_SESSION_TOKEN,
        }
    creds = _load_creds()
    if creds is None:
        creds = _sso_login()
    return creds


# ---------------------------------------------------------------------------
# Cliente bedrock-runtime
# ---------------------------------------------------------------------------
_runtime_client = None


def get_client(force_refresh: bool = False):
    """Retorna (e memoiza) um cliente bedrock-runtime autenticado."""
    global _runtime_client
    if _runtime_client is not None and not force_refresh:
        return _runtime_client
    creds = _resolve_credentials()
    _runtime_client = boto3.client(
        "bedrock-runtime",
        region_name=config.AWS_REGION,
        aws_access_key_id=creds.get("accessKeyId"),
        aws_secret_access_key=creds.get("secretAccessKey"),
        aws_session_token=creds.get("sessionToken"),
    )
    return _runtime_client


def testar_conexao() -> bool:
    """Faz uma chamada mínima ao modelo para validar credenciais/permissões."""
    resposta = _invocar_modelo("system de teste", 'Responda apenas: {"ok": true}')
    return bool(resposta)


# ---------------------------------------------------------------------------
# Invocação do modelo (Converse com fallback para invoke_model/Llama)
# ---------------------------------------------------------------------------
def _converse(client, system_prompt: str, user_prompt: str) -> str:
    """Usa a Converse API (agnóstica ao provedor; suporta LLaMA)."""
    resp = client.converse(
        modelId=config.BEDROCK_MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={
            "maxTokens": config.BEDROCK_MAX_TOKENS,
            "temperature": config.BEDROCK_TEMPERATURE,
        },
    )
    return resp["output"]["message"]["content"][0]["text"]


def _invoke_llama(client, system_prompt: str, user_prompt: str) -> str:
    """Fallback usando invoke_model no formato de prompt do LLaMA (Meta)."""
    prompt = (
        "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
        f"{system_prompt}\n<|eot_id|>"
        "<|start_header_id|>user<|end_header_id|>\n"
        f"{user_prompt}\n<|eot_id|>"
        "<|start_header_id|>assistant<|end_header_id|>\n"
    )
    resp = client.invoke_model(
        modelId=config.BEDROCK_MODEL_ID,
        body=json.dumps(
            {
                "prompt": prompt,
                "max_gen_len": config.BEDROCK_MAX_TOKENS,
                "temperature": config.BEDROCK_TEMPERATURE,
            }
        ),
        contentType="application/json",
    )
    return json.loads(resp["body"].read())["generation"]


def _mapear_erro_aws(exc: Exception) -> BedrockError:
    """Converte erros do boto3 em exceções de domínio com mensagens claras."""
    if isinstance(exc, NoCredentialsError):
        return BedrockAuthError(
            "Credenciais AWS não encontradas. Rode o login SSO ou configure as "
            "chaves no .env."
        )
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "AccessDenied"):
            return BedrockPermissionError(
                "Sem permissão para invocar o modelo Bedrock "
                f"'{config.BEDROCK_MODEL_ID}'. Verifique as permissões do papel."
            )
        if code in ("ExpiredTokenException", "UnrecognizedClientException",
                    "InvalidSignatureException"):
            return BedrockAuthError(
                "Credenciais AWS expiradas ou inválidas. Refaça o login SSO."
            )
        if code in ("ResourceNotFoundException", "ValidationException"):
            return BedrockModelError(
                f"Modelo '{config.BEDROCK_MODEL_ID}' indisponível nesta região "
                f"({config.AWS_REGION}). Detalhe: {exc}"
            )
        if code in ("ThrottlingException", "ServiceUnavailableException",
                    "ModelTimeoutException", "ModelNotReadyException"):
            return BedrockModelError(
                "Modelo Bedrock temporariamente indisponível (throttling). "
                "Tente novamente em instantes."
            )
        return BedrockError(f"Erro do Bedrock: {exc}")
    if isinstance(exc, BotoCoreError):
        return BedrockError(f"Erro de comunicação com a AWS: {exc}")
    return BedrockError(str(exc))


def _invocar_modelo(system_prompt: str, user_prompt: str) -> str:
    """Invoca o modelo tentando primeiro Converse e depois invoke_model.

    Reautentica automaticamente uma vez em caso de credenciais expiradas.
    """
    for tentativa in range(2):
        client = get_client(force_refresh=(tentativa == 1))
        try:
            try:
                return _converse(client, system_prompt, user_prompt)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                # Se Converse não for suportado para o modelo, tenta invoke_model.
                if code in ("ValidationException", "ResourceNotFoundException"):
                    return _invoke_llama(client, system_prompt, user_prompt)
                raise
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ExpiredTokenException", "UnrecognizedClientException") and tentativa == 0:
                # Credenciais expiraram: limpa cache e tenta novamente.
                try:
                    os.remove(config.BEDROCK_CREDS_CACHE)
                except OSError:
                    pass
                continue
            raise _mapear_erro_aws(exc) from exc
        except (BotoCoreError, NoCredentialsError) as exc:
            raise _mapear_erro_aws(exc) from exc
    raise BedrockModelError("Não foi possível invocar o modelo após nova tentativa.")


# ---------------------------------------------------------------------------
# Extração robusta do JSON retornado pelo modelo
# ---------------------------------------------------------------------------
_CAMPOS_PADRAO = {
    "acao": "pergunta_nao_suportada",
    "tabela": None,
    "coluna": None,
    "filtro": None,
    "limite": None,
}


def _extrair_json(texto: str) -> Dict[str, Any]:
    """Extrai o primeiro objeto JSON válido do texto retornado pelo modelo.

    Modelos abertos às vezes adicionam texto antes/depois do JSON; por isso
    procuramos o trecho entre o primeiro '{' e o '}' correspondente.
    """
    if not texto:
        return dict(_CAMPOS_PADRAO)

    # Tentativa direta.
    try:
        return _normalizar(json.loads(texto))
    except json.JSONDecodeError:
        pass

    # Procura um bloco {...} balanceado.
    inicio = texto.find("{")
    if inicio == -1:
        return dict(_CAMPOS_PADRAO)
    profundidade = 0
    for i in range(inicio, len(texto)):
        if texto[i] == "{":
            profundidade += 1
        elif texto[i] == "}":
            profundidade -= 1
            if profundidade == 0:
                bloco = texto[inicio : i + 1]
                try:
                    return _normalizar(json.loads(bloco))
                except json.JSONDecodeError:
                    break
    return dict(_CAMPOS_PADRAO)


def _normalizar(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Garante todos os campos esperados e valida a ação contra a allowlist."""
    resultado = dict(_CAMPOS_PADRAO)
    if isinstance(obj, dict):
        for k in _CAMPOS_PADRAO:
            if k in obj:
                resultado[k] = obj[k]
    acao = resultado.get("acao")
    if acao not in prompts.ACOES_PERMITIDAS:
        resultado["acao"] = "pergunta_nao_suportada"
    # Normaliza limite para int quando possível.
    limite = resultado.get("limite")
    if isinstance(limite, str) and limite.isdigit():
        resultado["limite"] = int(limite)
    elif not isinstance(limite, int):
        resultado["limite"] = None
    return resultado


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def interpretar_pergunta(pergunta: str) -> Dict[str, Any]:
    """Envia a pergunta ao Bedrock e devolve o JSON de intenção normalizado.

    Retorno garantido: dict com chaves acao, tabela, coluna, filtro, limite.
    Levanta BedrockError (ou subclasse) em caso de falha de AWS/modelo.
    """
    system_prompt = prompts.build_system_prompt(config.VIEW_NAME, config.VARIAVEIS_ESCOPO)
    user_prompt = prompts.build_user_prompt(pergunta)
    texto = _invocar_modelo(system_prompt, user_prompt)
    return _extrair_json(texto)


# ---------------------------------------------------------------------------
# Modo NL->SQL: escolha de tabelas, geração de SQL e resumo do resultado
# ---------------------------------------------------------------------------
def _extrair_lista_json(texto: str) -> list:
    """Extrai o primeiro array JSON de uma resposta do modelo."""
    if not texto:
        return []
    inicio, fim = texto.find("["), texto.rfind("]")
    if inicio == -1 or fim == -1 or fim < inicio:
        return []
    try:
        dados = json.loads(texto[inicio : fim + 1])
        return [str(x) for x in dados] if isinstance(dados, list) else []
    except json.JSONDecodeError:
        return []


def _limpar_sql(texto: str) -> str:
    """Remove blocos markdown e prefixos, retornando apenas a consulta SQL."""
    if not texto:
        return ""
    t = texto.strip()
    # Remove cercas de código ```sql ... ```
    t = re.sub(r"```(?:sql)?", "", t, flags=re.IGNORECASE).strip()
    # Remove prefixo "SQL:" eventual.
    t = re.sub(r"^\s*sql\s*:\s*", "", t, flags=re.IGNORECASE)
    # Mantém do primeiro SELECT/WITH em diante.
    m = re.search(r"\b(SELECT|WITH)\b", t, flags=re.IGNORECASE)
    if m:
        t = t[m.start():]
    return t.strip().rstrip(";").strip()


def escolher_tabelas(pergunta: str, tabelas: list) -> list:
    """Pede ao modelo as tabelas relevantes para a pergunta (máx. 4)."""
    user_prompt = prompts.build_table_selection_prompt(pergunta, tabelas)
    texto = _invocar_modelo("Você seleciona tabelas relevantes de um banco.", user_prompt)
    escolhidas = _extrair_lista_json(texto)
    # Mantém apenas nomes que existem na lista informada (case-insensitive).
    validas = {t.lower(): t for t in tabelas}
    return [validas[e.lower()] for e in escolhidas if e.lower() in validas][:4]


def gerar_sql(pergunta: str, schema_texto: str, max_linhas: int) -> str:
    """Gera uma consulta SELECT (somente leitura) a partir da pergunta + esquema."""
    system_prompt = prompts.SQL_SYSTEM_PROMPT.format(max_linhas=max_linhas)
    user_prompt = prompts.build_sql_generation_prompt(pergunta, schema_texto, max_linhas)
    texto = _invocar_modelo(system_prompt, user_prompt)
    return _limpar_sql(texto)


def resumir_resposta(pergunta: str, tabela_texto: str) -> str:
    """Resume o resultado da consulta em linguagem natural (best-effort)."""
    try:
        user_prompt = prompts.build_resumo_prompt(pergunta, tabela_texto)
        return _invocar_modelo("Você resume resultados de consultas de forma clara.",
                               user_prompt).strip()
    except BedrockError:
        return ""


if __name__ == "__main__":  # teste manual rápido
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    q = " ".join(sys.argv[1:]) or "Quantas matrículas no ensino médio por região?"
    print("Pergunta:", q)
    print("Intenção:", json.dumps(interpretar_pergunta(q), ensure_ascii=False, indent=2))
