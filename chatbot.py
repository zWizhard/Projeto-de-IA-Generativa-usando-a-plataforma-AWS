"""
chatbot.py
==========
Orquestra o fluxo do chatbot:

  1. Recebe a pergunta do usuário (linguagem natural).
  2. Envia ao Amazon Bedrock, que devolve apenas a INTENÇÃO em JSON.
  3. Interpreta o JSON e chama a FUNÇÃO PYTHON SEGURA correspondente em database.py.
  4. Formata a resposta final em texto claro (e, quando útil, uma tabela).

Importante: o Bedrock nunca gera nem executa SQL. Toda consulta é feita por
funções Python validadas (allowlist + somente leitura).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

import bedrock_client
import config
import database


# ---------------------------------------------------------------------------
# Utilidades de formatação
# ---------------------------------------------------------------------------
def _num(n: Any) -> str:
    """Formata inteiros no padrão brasileiro (separador de milhar com ponto)."""
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(n)


def _tabela_para_texto(linhas: List[Dict[str, Any]], col_rotulo: str, col_valor: str,
                       limite: int = 30) -> str:
    partes = []
    for linha in linhas[:limite]:
        partes.append(f"{linha[col_rotulo]}: {_num(linha[col_valor])}")
    return "; ".join(partes)


def _resp(texto: str, tabela: Optional[Dict[str, Any]] = None,
          intencao: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"texto": texto, "tabela": tabela, "intencao": intencao}


def _tabela_alvo(intencao: Dict[str, Any]) -> str:
    """Tabela alvo de ações de metadados (default: a VIEW do escopo)."""
    return intencao.get("tabela") or config.VIEW_NAME


# ---------------------------------------------------------------------------
# Handlers de cada ação
# ---------------------------------------------------------------------------
def _h_listar_tabelas(intencao: Dict[str, Any]) -> Dict[str, Any]:
    itens = database.listar_tabelas()
    escopo = [i["nome"] for i in itens if i["no_escopo"]]
    texto = (
        "As tabelas/visões do escopo deste projeto são: "
        + ", ".join(escopo)
        + f". No total, o banco possui {len(itens)} tabelas/visões."
    )
    return _resp(texto, {"colunas": ["nome", "tipo", "no_escopo"],
                         "linhas": itens}, intencao)


def _h_contar_colunas(intencao: Dict[str, Any]) -> Dict[str, Any]:
    tabela = _tabela_alvo(intencao)
    n = database.contar_colunas(tabela)
    return _resp(f"A tabela/base '{tabela}' possui {n} colunas.", intencao=intencao)


def _h_listar_colunas(intencao: Dict[str, Any]) -> Dict[str, Any]:
    tabela = _tabela_alvo(intencao)
    cols = database.listar_colunas(tabela)
    nomes = ", ".join(c["nome"] for c in cols)
    return _resp(
        f"A base '{tabela}' possui as seguintes colunas: {nomes}.",
        {"colunas": ["nome", "tipo"], "linhas": cols}, intencao,
    )


def _h_contar_registros(intencao: Dict[str, Any]) -> Dict[str, Any]:
    tabela = _tabela_alvo(intencao)
    n = database.contar_registros(tabela)
    return _resp(f"A tabela/base '{tabela}' possui {_num(n)} registros.", intencao=intencao)


def _h_descrever_tabela(intencao: Dict[str, Any]) -> Dict[str, Any]:
    tabela = _tabela_alvo(intencao)
    desc = database.descrever_tabela(tabela)
    pk = ", ".join(desc["chave_primaria"]) or "(sem chave primária declarada)"
    linhas = [{"coluna": c["nome"], "tipo": c["tipo"],
               "permite_nulo": "sim" if c["permite_nulo"] else "não",
               "pk": "sim" if c["chave_primaria"] else ""}
              for c in desc["colunas"]]
    texto = (f"A tabela '{desc['tabela']}' possui {len(desc['colunas'])} colunas. "
             f"Chave primária: {pk}.")
    return _resp(texto, {"colunas": ["coluna", "tipo", "permite_nulo", "pk"],
                         "linhas": linhas}, intencao)


def _h_mostrar_amostra(intencao: Dict[str, Any]) -> Dict[str, Any]:
    tabela = _tabela_alvo(intencao)
    limite = intencao.get("limite") or 5
    amostra = database.mostrar_amostra(tabela, limite=limite)
    return _resp(
        f"Amostra de {len(amostra['linhas'])} registros da base '{tabela}':",
        {"colunas": amostra["colunas"], "linhas": amostra["linhas"]}, intencao,
    )


def _h_identificar_chave_primaria(intencao: Dict[str, Any]) -> Dict[str, Any]:
    tabela = _tabela_alvo(intencao)
    pk = database.identificar_chave_primaria(tabela)
    if not pk:
        return _resp(f"A tabela '{tabela}' não possui chave primária declarada.", intencao=intencao)
    return _resp(f"A chave primária da tabela '{tabela}' é: {', '.join(pk)}.", intencao=intencao)


def _h_identificar_relacionamentos(intencao: Dict[str, Any]) -> Dict[str, Any]:
    # Sem tabela específica, descreve o relacionamento de toda a base do escopo.
    tabela = intencao.get("tabela") or config.VIEW_NAME
    rels = database.identificar_relacionamentos(tabela)
    if not rels:
        return _resp(f"A tabela '{tabela}' não possui chaves estrangeiras declaradas.",
                     intencao=intencao)
    linhas = [{"colunas": ", ".join(r["colunas"] or []),
               "referencia": f"{r['referencia_tabela']}({', '.join(r['referencia_colunas'] or [])})"}
              for r in rels]
    desc = "; ".join(f"{l['colunas']} -> {l['referencia']}" for l in linhas)
    return _resp(f"Relacionamentos da tabela '{tabela}': {desc}.",
                 {"colunas": ["colunas", "referencia"], "linhas": linhas}, intencao)


def _h_listar_variaveis_escopo(intencao: Dict[str, Any]) -> Dict[str, Any]:
    variaveis = database.listar_variaveis_escopo()
    nomes = ", ".join(v["variavel"] for v in variaveis)
    return _resp(f"As variáveis priorizadas na base final ({config.VIEW_NAME}) são: {nomes}.",
                 {"colunas": ["variavel"], "linhas": variaveis}, intencao)


def _etapa_da_intencao(intencao: Dict[str, Any]) -> Optional[str]:
    """Extrai a coluna de etapa (QT_MAT_*) da intenção, se houver."""
    coluna = (intencao.get("coluna") or "").upper().strip()
    if coluna in config.COLUNAS_ETAPA.values():
        return coluna
    return None


def _h_total_matriculas_por_municipio(intencao: Dict[str, Any]) -> Dict[str, Any]:
    limite = intencao.get("limite") or 15
    res = database.consultar_total_matriculas_por_municipio(limite=limite)
    texto = (f"Total de matrículas por município (top {len(res['linhas'])}): "
             + _tabela_para_texto(res["linhas"], "grupo", "total_matriculas"))
    return _resp(texto, res, intencao)


def _h_total_matriculas_por_uf(intencao: Dict[str, Any]) -> Dict[str, Any]:
    etapa = _etapa_da_intencao(intencao)
    res = database.consultar_total_matriculas_por_uf(limite=intencao.get("limite"),
                                                     coluna_etapa=etapa)
    contexto = f" ({etapa})" if etapa else ""
    texto = (f"Total de matrículas por UF{contexto}: "
             + _tabela_para_texto(res["linhas"], "grupo", "total_matriculas"))
    return _resp(texto, res, intencao)


def _h_total_matriculas_por_regiao(intencao: Dict[str, Any]) -> Dict[str, Any]:
    etapa = _etapa_da_intencao(intencao)
    res = database.consultar_total_matriculas_por_regiao(coluna_etapa=etapa)
    contexto = f" ({etapa})" if etapa else ""
    texto = (f"Total de matrículas por região{contexto}: "
             + _tabela_para_texto(res["linhas"], "grupo", "total_matriculas"))
    return _resp(texto, res, intencao)


def _h_matriculas_por_etapa(intencao: Dict[str, Any]) -> Dict[str, Any]:
    etapa = _etapa_da_intencao(intencao)
    res = database.consultar_matriculas_por_etapa_ensino(coluna=etapa)
    texto = ("Matrículas por etapa de ensino: "
             + _tabela_para_texto(res["linhas"], "etapa", "total_matriculas"))
    return _resp(texto, res, intencao)


def _h_matriculas_por_genero(intencao: Dict[str, Any]) -> Dict[str, Any]:
    res = database.consultar_matriculas_por_genero()
    texto = "Matrículas por gênero: " + _tabela_para_texto(res["linhas"], "genero", "total_matriculas")
    return _resp(texto, res, intencao)


def _h_matriculas_por_raca_cor(intencao: Dict[str, Any]) -> Dict[str, Any]:
    res = database.consultar_matriculas_por_raca_cor()
    texto = "Matrículas por raça/cor: " + _tabela_para_texto(res["linhas"], "raca_cor", "total_matriculas")
    return _resp(texto, res, intencao)


def _h_distribuicao_por_localizacao(intencao: Dict[str, Any]) -> Dict[str, Any]:
    res = database.consultar_distribuicao_por_localizacao()
    partes = [f"{l['localizacao']}: {_num(l['escolas'])} escolas, {_num(l['matriculas'])} matrículas"
              for l in res["linhas"]]
    return _resp("Distribuição por localização — " + "; ".join(partes), res, intencao)


def _h_valores_distintos(intencao: Dict[str, Any]) -> Dict[str, Any]:
    coluna = intencao.get("coluna")
    if not coluna:
        return _resp("Para listar valores distintos, informe a coluna desejada "
                     "(ex.: TP_LOCALIZACAO).", intencao=intencao)
    res = database.consultar_valores_distintos(coluna)
    texto = (f"Valores distintos de '{coluna}': "
             + _tabela_para_texto(res["linhas"], "valor", "ocorrencias"))
    return _resp(texto, res, intencao)


def _h_resumo_estatistico(intencao: Dict[str, Any]) -> Dict[str, Any]:
    res = database.gerar_resumo_estatistico_escopo()
    partes = [f"{l['indicador']}: {_num(l['valor'])}" for l in res["linhas"]]
    return _resp("Resumo estatístico da base final — " + "; ".join(partes), res, intencao)


def _h_estatisticas_banco(intencao: Dict[str, Any]) -> Dict[str, Any]:
    est = database.estatisticas_banco()
    total_col = est.get("total_colunas")
    col_txt = f", totalizando {_num(total_col)} colunas (variáveis)" if total_col is not None else ""
    n_view = len(database.listar_variaveis_escopo())
    texto = (f"Considerando {est['abrangencia']} ({', '.join(est['tabelas'])}), "
             f"há {est['num_tabelas']} tabelas{col_txt}. "
             f"A base consolidada {config.VIEW_NAME} possui {n_view} colunas.")
    return _resp(texto, {"colunas": ["tabela"],
                         "linhas": [{"tabela": t} for t in est["tabelas"]]}, intencao)


def _resultado_para_texto(resultado: Dict[str, Any], max_linhas: int = 20) -> str:
    """Converte o resultado tabular em texto compacto (para resumo do modelo)."""
    colunas = resultado["colunas"]
    linhas = resultado["linhas"][:max_linhas]
    cabecalho = " | ".join(colunas)
    corpo = "\n".join(" | ".join(str(l.get(c, "")) for c in colunas) for l in linhas)
    return f"{cabecalho}\n{corpo}"


def _h_consulta_sql(intencao: Dict[str, Any]) -> Dict[str, Any]:
    """Modo NL->SQL: escolhe tabelas, gera SELECT seguro, executa e resume."""
    if not config.PERMITIR_SQL_LIVRE:
        return _h_nao_suportada(intencao)

    pergunta = (intencao.get("_pergunta") or intencao.get("filtro") or "").strip()
    if not pergunta:
        return _erro("Não entendi a pergunta para consultar o banco.", intencao)

    try:
        if config.PERMITIR_TODAS_TABELAS:
            # Modo amplo (todas as tabelas): seleciona tabelas + esquema real.
            tabelas = database.listar_todas_tabelas()
            escolhidas = bedrock_client.escolher_tabelas(pergunta, tabelas)
            if not escolhidas:
                termos = [t for t in re.findall(r"\w+", pergunta.lower()) if len(t) > 3]
                escolhidas = [t for t in tabelas
                              if any(termo in t.lower() for termo in termos)][:4]
            if not escolhidas:
                escolhidas = list(config.TABELAS_ESCOPO)
            schema_texto = database.descrever_schema_texto(escolhidas)
            sql = bedrock_client.gerar_sql(pergunta, schema_texto, config.MAX_LINHAS_SQL)
            if not sql:
                return _erro("Não consegui gerar uma consulta. Tente reformular.", intencao)
            resultado = database.executar_sql_seguro(sql)
            intencao = {**intencao, "_tabelas": escolhidas, "_sql_gerado": resultado["sql"]}
        else:
            # Modo escopo (padrão): consulta a base consolidada vw_escopo_ia.
            schema_texto = database.descrever_view_texto()
            sql = bedrock_client.gerar_sql(pergunta, schema_texto, config.MAX_LINHAS_SQL)
            if not sql:
                return _erro("Não consegui gerar uma consulta. Tente reformular.", intencao)
            resultado = database.executar_sql_escopo(sql)
            intencao = {**intencao, "_sql_gerado": resultado["sql"]}
    except database.BancoError as exc:
        intencao = {**intencao, "_sql_gerado": locals().get("sql")}
        return _erro(
            "A consulta gerada não pôde ser executada (a pergunta pode ser ambígua "
            f"ou referenciar colunas fora do escopo). Detalhe: {exc}", intencao,
        )

    if not resultado["linhas"]:
        return _resp("A consulta não retornou resultados.",
                     {"colunas": resultado["colunas"], "linhas": []}, intencao)

    # 4) Resumo em linguagem natural (best-effort) + tabela completa.
    resumo = bedrock_client.resumir_resposta(pergunta, _resultado_para_texto(resultado))
    texto = resumo or f"Resultado da consulta ({len(resultado['linhas'])} linha(s)):"
    return _resp(texto, {"colunas": resultado["colunas"], "linhas": resultado["linhas"]},
                 intencao)


def _h_nao_suportada(intencao: Dict[str, Any]) -> Dict[str, Any]:
    return _resp(
        "Não consegui entender a pergunta dentro do escopo do projeto. "
        "Tente, por exemplo: 'Quais são as colunas da base?', "
        "'Qual UF tem mais matrículas?' ou 'Matrículas por raça/cor'.",
        intencao=intencao,
    )


# Mapa ação -> handler.
_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "listar_tabelas": _h_listar_tabelas,
    "contar_colunas": _h_contar_colunas,
    "listar_colunas": _h_listar_colunas,
    "contar_registros": _h_contar_registros,
    "descrever_tabela": _h_descrever_tabela,
    "mostrar_amostra": _h_mostrar_amostra,
    "identificar_chave_primaria": _h_identificar_chave_primaria,
    "identificar_relacionamentos": _h_identificar_relacionamentos,
    "listar_variaveis_escopo": _h_listar_variaveis_escopo,
    "total_matriculas_por_municipio": _h_total_matriculas_por_municipio,
    "total_matriculas_por_uf": _h_total_matriculas_por_uf,
    "total_matriculas_por_regiao": _h_total_matriculas_por_regiao,
    "matriculas_por_etapa_ensino": _h_matriculas_por_etapa,
    "matriculas_por_genero": _h_matriculas_por_genero,
    "matriculas_por_raca_cor": _h_matriculas_por_raca_cor,
    "distribuicao_por_localizacao": _h_distribuicao_por_localizacao,
    "valores_distintos": _h_valores_distintos,
    "resumo_estatistico_escopo": _h_resumo_estatistico,
    "estatisticas_banco": _h_estatisticas_banco,
    "consulta_sql": _h_consulta_sql,
    "pergunta_nao_suportada": _h_nao_suportada,
}


# ---------------------------------------------------------------------------
# Fluxo principal
# ---------------------------------------------------------------------------
def processar_pergunta(pergunta: str) -> Dict[str, Any]:
    """Processa uma pergunta de ponta a ponta e devolve a resposta formatada.

    Retorno: {"texto": str, "tabela": dict|None, "intencao": dict|None,
              "erro": bool}
    Trata todos os erros previstos (AWS, Bedrock, banco, tabela/coluna inválida).
    """
    pergunta = (pergunta or "").strip()
    if not pergunta:
        return {"texto": "Digite uma pergunta para começar.", "tabela": None,
                "intencao": None, "erro": True}

    # 1) Interpretação da intenção via Bedrock.
    try:
        intencao = bedrock_client.interpretar_pergunta(pergunta)
    except bedrock_client.BedrockAuthError as exc:
        return _erro(f"Falha de autenticação na AWS: {exc}")
    except bedrock_client.BedrockPermissionError as exc:
        return _erro(f"Sem permissão no Bedrock: {exc}")
    except bedrock_client.BedrockModelError as exc:
        return _erro(f"Modelo Bedrock indisponível: {exc}")
    except bedrock_client.BedrockError as exc:
        return _erro(f"Erro ao consultar o Bedrock: {exc}")

    # 2) Dispatch para a função segura de banco.
    intencao["_pergunta"] = pergunta  # disponibiliza a pergunta original aos handlers
    acao = intencao.get("acao", "pergunta_nao_suportada")
    handler = _HANDLERS.get(acao, _h_nao_suportada)
    try:
        resultado = handler(intencao)
        resultado["erro"] = False
        return resultado
    except database.TabelaInvalidaError as exc:
        return _erro(f"Tabela inválida: {exc}", intencao)
    except database.ColunaInvalidaError as exc:
        return _erro(f"Coluna inválida: {exc}", intencao)
    except database.RelacionamentoError as exc:
        return _erro(f"Problema de relacionamento entre tabelas: {exc}", intencao)
    except database.BancoIndisponivelError as exc:
        return _erro(f"Banco de dados indisponível: {exc}", intencao)
    except database.BancoError as exc:
        return _erro(f"Erro ao consultar o banco: {exc}", intencao)
    except Exception as exc:  # rede de segurança
        return _erro(f"Erro inesperado: {exc}", intencao)


def _erro(mensagem: str, intencao: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"texto": mensagem, "tabela": None, "intencao": intencao, "erro": True}


def inicializar() -> Dict[str, Any]:
    """Inicializa conexão e plano do escopo (chamado pela interface no startup)."""
    database.conectar_banco()
    return database.inicializar_escopo()


if __name__ == "__main__":  # modo CLI para teste rápido
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    inicializar()
    if len(sys.argv) > 1:
        r = processar_pergunta(" ".join(sys.argv[1:]))
        print(r["texto"])
    else:
        print("Chatbot INEP/Censo Escolar — digite 'sair' para encerrar.\n")
        while True:
            try:
                q = input("Você: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q or q.lower() in ("sair", "exit", "quit"):
                break
            print("\nBot:", processar_pergunta(q)["texto"], "\n")
