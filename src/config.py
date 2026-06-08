"""
config.py
=========
Carrega todas as configurações sensíveis e parâmetros do projeto a partir de
variáveis de ambiente (arquivo .env). Nenhuma credencial fica escrita no código.

Responsabilidades:
- Configuração da AWS / Amazon Bedrock (região, modelo, SSO).
- Configuração da conexão com o banco de dados (monta a URL do SQLAlchemy).
- Definição do escopo do projeto: tabelas permitidas (allowlist), nome da VIEW
  consolidada e a lista de variáveis priorizadas pelo professor.
- Overrides opcionais de chaves de junção e colunas descritivas, usados quando a
  detecção automática de relacionamentos não for possível.

Decisão de projeto: o código é agnóstico ao SGBD. Basta ajustar DB_ENGINE e as
demais variáveis no .env (PostgreSQL, MySQL ou SQLite). Isso evita "inventar"
nomes de driver e mantém o mesmo código para qualquer banco disponível.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# Raiz do projeto = pasta-pai de src/ (onde fica o arquivo .env).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    # python-dotenv é opcional em produção (as variáveis podem vir do ambiente),
    # mas facilita o desenvolvimento local. Carrega o .env da raiz do projeto,
    # independentemente do diretório de trabalho atual.
    from dotenv import load_dotenv

    _env_path = PROJECT_ROOT / ".env"
    load_dotenv(_env_path if _env_path.exists() else None)
except Exception:  # pragma: no cover - ambiente sem python-dotenv
    pass


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    """Lê uma variável de ambiente, tratando string vazia como ausente."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


# ---------------------------------------------------------------------------
# Amazon Bedrock / AWS
# ---------------------------------------------------------------------------
# Região onde o bedrock-runtime será invocado. O escopo do professor pede
# us-east-2; deixamos configurável caso o modelo esteja em outra região.
AWS_REGION: str = _get("AWS_REGION", "us-east-2")

# O IAM Identity Center (SSO) do curso está hospedado em us-east-1. O login SSO
# precisa apontar para essa região mesmo que o Bedrock rode em us-east-2.
SSO_REGION: str = _get("SSO_REGION", "us-east-1")
SSO_START_URL: str = _get("SSO_START_URL", "https://d-90663e488b.awsapps.com/start")
SSO_ACCOUNT_ID: str = _get("SSO_ACCOUNT_ID", "248189947068")
SSO_ROLE_NAME: str = _get("SSO_ROLE_NAME", "BedrockFullAccess")

# Modelo padrão: o LLaMA 4 Scout já testado no ambiente do curso. É um perfil de
# inferência multi-região (prefixo "us."), então funciona em us-east-1/us-east-2.
BEDROCK_MODEL_ID: str = _get("BEDROCK_MODEL_ID", "us.meta.llama4-scout-17b-instruct-v1:0")

# Caminho do cache de credenciais temporárias do SSO (mesmo formato do
# test_bedrock.py original do curso).
BEDROCK_CREDS_CACHE: str = _get(
    "BEDROCK_CREDS_CACHE", os.path.expanduser("~/.bedrock_creds.json")
)

# Parâmetros de geração do modelo. Temperatura baixa => respostas determinísticas,
# o que é desejável para um classificador que precisa devolver JSON estável.
BEDROCK_TEMPERATURE: float = float(_get("BEDROCK_TEMPERATURE", "0.0"))
BEDROCK_MAX_TOKENS: int = int(_get("BEDROCK_MAX_TOKENS", "512"))

# Credenciais estáticas opcionais (caso o usuário prefira chaves diretas em vez
# do fluxo SSO). Se preenchidas, têm prioridade sobre o login interativo.
AWS_ACCESS_KEY_ID: Optional[str] = _get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY: Optional[str] = _get("AWS_SECRET_ACCESS_KEY")
AWS_SESSION_TOKEN: Optional[str] = _get("AWS_SESSION_TOKEN")


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------
DB_ENGINE: str = _get("DB_ENGINE", "postgresql").lower()  # postgresql | mysql | sqlite
DB_HOST: Optional[str] = _get("DB_HOST")
DB_PORT: Optional[str] = _get("DB_PORT")
DB_NAME: Optional[str] = _get("DB_NAME", "iesb")
DB_USER: Optional[str] = _get("DB_USER")
DB_PASSWORD: Optional[str] = _get("DB_PASSWORD")

# Schema onde estão as tabelas do escopo (PostgreSQL usa "public" por padrão).
DB_SCHEMA: Optional[str] = _get("DB_SCHEMA", "public")

# Permite passar a URL completa do SQLAlchemy de uma vez (tem prioridade).
DATABASE_URL_OVERRIDE: Optional[str] = _get("DATABASE_URL")

# Drivers padrão por SGBD. Mantém o código agnóstico sem "inventar" drivers.
_DRIVERS: Dict[str, str] = {
    "postgresql": "postgresql+psycopg2",
    "postgres": "postgresql+psycopg2",
    "mysql": "mysql+pymysql",
    "mariadb": "mysql+pymysql",
    "sqlite": "sqlite",
}

_DEFAULT_PORTS: Dict[str, str] = {
    "postgresql": "5432",
    "postgres": "5432",
    "mysql": "3306",
    "mariadb": "3306",
}


def build_database_url() -> str:
    """Monta a URL de conexão do SQLAlchemy a partir das variáveis de ambiente.

    Prioridade: DATABASE_URL (se definida) > variáveis DB_*.
    Para SQLite, DB_NAME é interpretado como caminho do arquivo .db.
    """
    if DATABASE_URL_OVERRIDE:
        return DATABASE_URL_OVERRIDE

    driver = _DRIVERS.get(DB_ENGINE)
    if driver is None:
        raise ValueError(
            f"DB_ENGINE '{DB_ENGINE}' não suportado. "
            f"Use um de: {', '.join(sorted(set(_DRIVERS)))}."
        )

    if DB_ENGINE == "sqlite":
        # sqlite:///arquivo.db (caminho relativo) ou caminho absoluto.
        db_path = DB_NAME or "censo_escolar.db"
        return f"sqlite:///{db_path}"

    if not all([DB_HOST, DB_NAME, DB_USER]):
        raise ValueError(
            "Conexão incompleta: defina DB_HOST, DB_NAME e DB_USER no .env "
            "(ou forneça DATABASE_URL)."
        )

    port = DB_PORT or _DEFAULT_PORTS.get(DB_ENGINE, "")
    auth = DB_USER or ""
    if DB_PASSWORD:
        auth = f"{DB_USER}:{DB_PASSWORD}"
    host = DB_HOST if not port else f"{DB_HOST}:{port}"
    return f"{driver}://{auth}@{host}/{DB_NAME}"


# ---------------------------------------------------------------------------
# Escopo do projeto (definido pelo docente)
# ---------------------------------------------------------------------------
# Nome da VIEW consolidada que serve de base principal ao chatbot.
VIEW_NAME: str = _get("VIEW_NAME", "vw_escopo_ia")

# Tabelas do escopo. A principal é inep_censo_escolar; as demais são dimensionais.
TABELA_FATO: str = _get("TABELA_FATO", "inep_censo_escolar")
TABELA_MUNICIPIO: str = _get("TABELA_MUNICIPIO", "municipio")
TABELA_UF: str = _get("TABELA_UF", "unidade_federacao")
TABELA_REGIAO: str = _get("TABELA_REGIAO", "regiao")  # "região" -> identificador ASCII

# Tabela-detalhe das matrículas. Descoberta na inspeção do banco real: as
# variáveis QT_MAT_* não estão em inep_censo_escolar, mas nesta tabela, que se
# relaciona 1:1 com a fato por (nu_ano_censo, co_entidade).
TABELA_MATRICULA: str = _get("TABELA_MATRICULA", "inep_censo_escolar_matricula")

TABELAS_ESCOPO: List[str] = [
    TABELA_FATO,
    TABELA_MATRICULA,
    TABELA_MUNICIPIO,
    TABELA_UF,
    TABELA_REGIAO,
]

# Allowlist de tabelas/views do escopo (caminho otimizado/determinístico).
ALLOWLIST_TABELAS: List[str] = [VIEW_NAME] + TABELAS_ESCOPO

# Cobertura do projeto:
# - PERMITIR_SQL_LIVRE: habilita o modo NL->SQL, no qual o Bedrock gera uma consulta
#   SELECT (somente leitura) validada e executada pelo Python. Isso permite responder
#   QUALQUER pergunta sobre os DADOS, porém SEMPRE limitado às tabelas do escopo.
# - PERMITIR_TODAS_TABELAS: se True, libera metadados/consultas para QUALQUER tabela
#   do banco. Por padrão é False — o sistema responde apenas sobre as tabelas do
#   escopo definido no prompt (inep_censo_escolar, inep_censo_escolar_matricula,
#   municipio, unidade_federacao, regiao).
# Em todos os casos o sistema é 100% somente leitura (nunca executa comandos destrutivos).
PERMITIR_TODAS_TABELAS: bool = _get("PERMITIR_TODAS_TABELAS", "false").lower() in ("1", "true", "sim", "yes")
PERMITIR_SQL_LIVRE: bool = _get("PERMITIR_SQL_LIVRE", "true").lower() in ("1", "true", "sim", "yes")

# Limite máximo de linhas retornadas por uma consulta NL->SQL livre.
MAX_LINHAS_SQL: int = int(_get("MAX_LINHAS_SQL", "200"))

# Variáveis priorizadas pelo professor. Apenas estas colunas de medida/categoria
# entram na VIEW (além das colunas descritivas nome_municipio/nome_uf/nome_regiao).
VARIAVEIS_ESCOPO: List[str] = [
    "TP_CATEGORIA_ESCOLA_PRIVADA",
    "TP_LOCALIZACAO",
    "TP_LOCALIZACAO_DIFERENCIADA",
    "QT_MAT_INF",
    "QT_MAT_FUND",
    "QT_MAT_MED",
    "QT_MAT_BAS_FEM",
    "QT_MAT_BAS_MASC",
    "QT_MAT_BAS_ND",
    "QT_MAT_BAS_BRANCA",
    "QT_MAT_BAS_PRETA",
    "QT_MAT_BAS_PARDA",
    "QT_MAT_BAS_AMARELA",
    "QT_MAT_BAS_INDIGENA",
]

# Colunas de medida (numéricas) por etapa de ensino e por recorte demográfico.
COLUNAS_ETAPA = {
    "infantil": "QT_MAT_INF",
    "fundamental": "QT_MAT_FUND",
    "medio": "QT_MAT_MED",
}
COLUNAS_GENERO = {
    "feminino": "QT_MAT_BAS_FEM",
    "masculino": "QT_MAT_BAS_MASC",
}
COLUNAS_RACA_COR = {
    "nao_declarada": "QT_MAT_BAS_ND",
    "branca": "QT_MAT_BAS_BRANCA",
    "preta": "QT_MAT_BAS_PRETA",
    "parda": "QT_MAT_BAS_PARDA",
    "amarela": "QT_MAT_BAS_AMARELA",
    "indigena": "QT_MAT_BAS_INDIGENA",
}
COLUNAS_LOCALIZACAO = ["TP_LOCALIZACAO", "TP_LOCALIZACAO_DIFERENCIADA"]

# Nomes (estáveis) das colunas descritivas dentro da VIEW.
COL_NOME_MUNICIPIO_VIEW = "nome_municipio"
COL_NOME_UF_VIEW = "nome_uf"
COL_NOME_REGIAO_VIEW = "nome_regiao"


# ---------------------------------------------------------------------------
# Overrides de relacionamento (usados quando a detecção automática falhar)
# ---------------------------------------------------------------------------
@dataclass
class OverridesRelacionamento:
    """Permite que o usuário informe manualmente as chaves de ligação e as
    colunas descritivas, caso a introspecção automática não as encontre.

    Todos os campos são opcionais e lidos do .env. Quando ausentes, o módulo
    database.py tenta descobrir os relacionamentos via chaves estrangeiras e,
    em último caso, por heurística de nomes.
    """

    # Coluna em inep_censo_escolar que liga ao município / uf / região.
    fk_municipio: Optional[str] = field(default_factory=lambda: _get("FK_MUNICIPIO"))
    fk_uf: Optional[str] = field(default_factory=lambda: _get("FK_UF"))
    fk_regiao: Optional[str] = field(default_factory=lambda: _get("FK_REGIAO"))

    # Coluna chave (PK) nas tabelas dimensionais.
    pk_municipio: Optional[str] = field(default_factory=lambda: _get("PK_MUNICIPIO"))
    pk_uf: Optional[str] = field(default_factory=lambda: _get("PK_UF"))
    pk_regiao: Optional[str] = field(default_factory=lambda: _get("PK_REGIAO"))

    # Coluna descritiva (nome) em cada tabela dimensional.
    nome_municipio: Optional[str] = field(default_factory=lambda: _get("COL_NOME_MUNICIPIO"))
    nome_uf: Optional[str] = field(default_factory=lambda: _get("COL_NOME_UF"))
    nome_regiao: Optional[str] = field(default_factory=lambda: _get("COL_NOME_REGIAO"))

    # Ligação dimensional adicional (ex.: município -> uf -> região), opcional.
    fk_municipio_uf: Optional[str] = field(default_factory=lambda: _get("FK_MUNICIPIO_UF"))
    fk_uf_regiao: Optional[str] = field(default_factory=lambda: _get("FK_UF_REGIAO"))


OVERRIDES = OverridesRelacionamento()
