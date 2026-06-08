"""
database.py
===========
Camada de acesso ao banco de dados (PostgreSQL, mas agnóstica via SQLAlchemy).

Princípios de segurança aplicados:
- Apenas leitura: todas as consultas são SELECT. Comandos destrutivos (DROP,
  DELETE, UPDATE, INSERT, ALTER, TRUNCATE, GRANT, REVOKE) são bloqueados por um
  guarda explícito e nunca são gerados a partir da IA.
- Allowlist: o chatbot só consulta as tabelas/views do escopo (config.ALLOWLIST_TABELAS).
- Identificadores validados: nomes de tabela/coluna são resolvidos contra o
  schema real (introspecção) e citados (quoting) pelo dialeto, evitando injeção.
- Parâmetros: valores variáveis (limites, filtros) vão por parâmetros, não por
  concatenação de strings.

Sobre a VIEW vw_escopo_ia:
A inspeção do banco real mostrou que as variáveis do escopo estão distribuídas
em duas tabelas-fato relacionadas 1:1:
  - inep_censo_escolar            -> TP_CATEGORIA_ESCOLA_PRIVADA, TP_LOCALIZACAO,
                                     TP_LOCALIZACAO_DIFERENCIADA, co_municipio
  - inep_censo_escolar_matricula  -> QT_MAT_* (matrículas)
As dimensões municipio/unidade_federacao/regiao se ligam por chaves estrangeiras
declaradas. O sistema descobre esse plano por introspecção (não inventa chaves) e
cria a VIEW; caso o usuário do banco seja somente-leitura, usa uma "VIEW lógica"
(subconsulta equivalente) — mantendo o mesmo resultado de forma 100% read-only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError, OperationalError

import config


# ---------------------------------------------------------------------------
# Exceções de domínio
# ---------------------------------------------------------------------------
class BancoError(Exception):
    """Erro genérico de banco de dados (mensagem amigável)."""


class BancoIndisponivelError(BancoError):
    """Falha de conexão/autenticação com o banco."""


class TabelaInvalidaError(BancoError):
    """Tabela inexistente ou fora da allowlist."""


class ColunaInvalidaError(BancoError):
    """Coluna inexistente na tabela."""


class RelacionamentoError(BancoError):
    """Não foi possível identificar o relacionamento entre as tabelas."""


# ---------------------------------------------------------------------------
# Engine / conexão
# ---------------------------------------------------------------------------
_engine: Optional[Engine] = None


def conectar_banco() -> Engine:
    """Cria (e memoiza) o engine do SQLAlchemy e valida a conexão.

    Levanta BancoIndisponivelError se o banco não responder.
    """
    global _engine
    if _engine is not None:
        return _engine
    try:
        url = config.build_database_url()
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 20} if url.startswith("postgresql") else {},
        )
        # Testa a conexão imediatamente para falhar cedo, com mensagem clara.
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _engine = engine
        return _engine
    except (OperationalError, SQLAlchemyError) as exc:
        raise BancoIndisponivelError(
            "Não foi possível conectar ao banco de dados. Verifique host, porta, "
            f"usuário e senha no .env. Detalhe técnico: {str(exc)[:200]}"
        ) from exc


def _inspector():
    return inspect(conectar_banco())


def _quote(nome: str) -> str:
    """Cita um identificador conforme o dialeto (protege contra injeção/maiúsculas)."""
    return conectar_banco().dialect.identifier_preparer.quote(nome)


def _quote_tabela(nome: str) -> str:
    """Cita tabela qualificada pelo schema, quando houver."""
    if config.DB_SCHEMA:
        return f"{_quote(config.DB_SCHEMA)}.{_quote(nome)}"
    return _quote(nome)


def _run_select(sql: str, params: Optional[Dict[str, Any]] = None) -> Tuple[List[str], List[tuple]]:
    """Executa um SELECT em transação somente-leitura e retorna (colunas, linhas).

    Guarda de segurança: rejeita qualquer SQL que não comece por SELECT/WITH ou
    que contenha verbos destrutivos.
    """
    _assert_select_only(sql)
    engine = conectar_banco()
    try:
        with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                # Reforça leitura no nível da transação.
                conn.execute(text("SET TRANSACTION READ ONLY"))
            result = conn.execute(text(sql), params or {})
            colunas = list(result.keys())
            linhas = result.fetchall()
            return colunas, [tuple(r) for r in linhas]
    except SQLAlchemyError as exc:
        raise BancoError(f"Erro ao executar consulta: {str(exc)[:200]}") from exc


_DESTRUTIVO = re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|GRANT|REVOKE|CREATE|REPLACE|"
    r"MERGE|CALL|EXECUTE|COPY)\b",
    re.IGNORECASE,
)


def _assert_select_only(sql: str) -> None:
    limpo = sql.strip().lstrip("(").strip()
    if not re.match(r"^(SELECT|WITH)\b", limpo, re.IGNORECASE):
        raise BancoError("Consulta bloqueada: apenas SELECT é permitido.")
    if _DESTRUTIVO.search(sql):
        raise BancoError("Consulta bloqueada: contém comando potencialmente destrutivo.")


# ---------------------------------------------------------------------------
# Validação de identificadores (resolução case-insensitive contra o schema real)
# ---------------------------------------------------------------------------
def _tabelas_reais() -> Dict[str, str]:
    """Mapa {nome_lower: nome_real} de todas as tabelas e views do schema."""
    insp = _inspector()
    nomes: List[str] = []
    nomes += insp.get_table_names(schema=config.DB_SCHEMA)
    nomes += insp.get_view_names(schema=config.DB_SCHEMA)
    return {n.lower(): n for n in nomes}


def validar_tabela(nome_tabela: str, restringir_allowlist: bool = True) -> str:
    """Resolve o nome real da tabela (case-insensitive) e valida allowlist.

    Retorna o nome real. Levanta TabelaInvalidaError se não existir ou se estiver
    fora do escopo permitido (quando restringir_allowlist=True).
    """
    if not nome_tabela:
        raise TabelaInvalidaError("Nenhuma tabela informada.")
    reais = _tabelas_reais()
    chave = nome_tabela.strip().lower()
    if chave not in reais:
        raise TabelaInvalidaError(
            f"A tabela '{nome_tabela}' não existe no banco "
            f"(schema {config.DB_SCHEMA})."
        )
    # A allowlist só é aplicada quando o modo "todas as tabelas" está desligado.
    if restringir_allowlist and not config.PERMITIR_TODAS_TABELAS:
        permitidas = {t.lower() for t in config.ALLOWLIST_TABELAS}
        if chave not in permitidas:
            raise TabelaInvalidaError(
                f"A tabela '{nome_tabela}' está fora do escopo permitido. "
                f"Tabelas do escopo: {', '.join(config.ALLOWLIST_TABELAS)}."
            )
    return reais[chave]


def _colunas_reais(nome_tabela_real: str) -> Dict[str, str]:
    """Mapa {nome_lower: nome_real} das colunas de uma tabela/view."""
    insp = _inspector()
    try:
        cols = insp.get_columns(nome_tabela_real, schema=config.DB_SCHEMA)
    except SQLAlchemyError as exc:
        raise TabelaInvalidaError(f"Não foi possível ler colunas de '{nome_tabela_real}': {exc}")
    return {c["name"].lower(): c["name"] for c in cols}


def validar_coluna(nome_tabela: str, nome_coluna: str) -> str:
    """Resolve e valida o nome real de uma coluna dentro de uma tabela/view."""
    chave = (nome_coluna or "").strip().lower()
    if _view_logica_ativa(nome_tabela):
        mapa = {c.lower(): c for c in _COLUNAS_VIEW}
        if chave not in mapa:
            raise ColunaInvalidaError(
                f"A coluna '{nome_coluna}' não existe na base final '{config.VIEW_NAME}'."
            )
        return mapa[chave]
    tabela_real = validar_tabela(nome_tabela)
    cols = _colunas_reais(tabela_real)
    if chave not in cols:
        raise ColunaInvalidaError(
            f"A coluna '{nome_coluna}' não existe na tabela '{nome_tabela}'."
        )
    return cols[chave]


# ---------------------------------------------------------------------------
# Funções de metadados (introspecção)
# ---------------------------------------------------------------------------
def listar_tabelas() -> List[Dict[str, Any]]:
    """Lista as tabelas do escopo (ou todas, se PERMITIR_TODAS_TABELAS=True)."""
    insp = _inspector()
    escopo = {t.lower() for t in config.ALLOWLIST_TABELAS}

    if config.PERMITIR_TODAS_TABELAS:
        itens: List[Dict[str, Any]] = []
        for nome in insp.get_table_names(schema=config.DB_SCHEMA):
            itens.append({"nome": nome, "tipo": "tabela", "no_escopo": nome.lower() in escopo})
        for nome in insp.get_view_names(schema=config.DB_SCHEMA):
            itens.append({"nome": nome, "tipo": "view", "no_escopo": nome.lower() in escopo})
        return sorted(itens, key=lambda x: (not x["no_escopo"], x["nome"]))

    # Modo escopo: apenas as tabelas definidas no prompt + a base consolidada.
    reais = _tabelas_reais()
    itens = []
    for nome in config.TABELAS_ESCOPO:
        real = reais.get(nome.lower())
        if real:
            itens.append({"nome": real, "tipo": "tabela", "no_escopo": True})
    # A base final (VIEW física ou lógica).
    itens.append({"nome": config.VIEW_NAME, "tipo": "view (base consolidada)",
                  "no_escopo": True})
    return itens


def _view_logica_ativa(nome_tabela: str) -> bool:
    """True quando o alvo é a VIEW do escopo e ela existe apenas de forma lógica."""
    if not _eh_alvo_view(nome_tabela):
        return False
    inicializar_escopo()
    return not _VIEW_FISICA


def _relacionamentos_logicos() -> List[Dict[str, Any]]:
    """Relacionamentos da VIEW lógica, derivados do plano de junção descoberto."""
    inicializar_escopo()
    p = _PLANO
    rels: List[Dict[str, Any]] = []
    if p is None:
        return rels
    if p.matricula and p.matricula_fato:
        rels.append({"colunas": [c[0] for c in p.matricula_fato],
                     "referencia_tabela": p.fato,
                     "referencia_colunas": [c[1] for c in p.matricula_fato]})
    if p.municipio and p.fato_municipio:
        rels.append({"colunas": [p.fato_municipio[0]], "referencia_tabela": p.municipio,
                     "referencia_colunas": [p.fato_municipio[1]]})
    if p.uf and p.municipio_uf:
        rels.append({"colunas": [p.municipio_uf[0]], "referencia_tabela": p.uf,
                     "referencia_colunas": [p.municipio_uf[1]]})
    if p.regiao and p.uf_regiao:
        rels.append({"colunas": [p.uf_regiao[0]], "referencia_tabela": p.regiao,
                     "referencia_colunas": [p.uf_regiao[1]]})
    return rels


def contar_colunas(nome_tabela: str) -> int:
    if _view_logica_ativa(nome_tabela):
        return len(_COLUNAS_VIEW)
    tabela_real = validar_tabela(nome_tabela)
    return len(_inspector().get_columns(tabela_real, schema=config.DB_SCHEMA))


def listar_colunas(nome_tabela: str) -> List[Dict[str, str]]:
    if _view_logica_ativa(nome_tabela):
        return [dict(c) for c in _COLUNAS_VIEW_INFO]
    tabela_real = validar_tabela(nome_tabela)
    cols = _inspector().get_columns(tabela_real, schema=config.DB_SCHEMA)
    return [{"nome": c["name"], "tipo": str(c["type"])} for c in cols]


def contar_registros(nome_tabela: str) -> int:
    if _view_logica_ativa(nome_tabela):
        _, linhas = _run_select(f"SELECT COUNT(*) FROM {_fonte_escopo()}")
        return int(linhas[0][0])
    tabela_real = validar_tabela(nome_tabela)
    colunas, linhas = _run_select(f"SELECT COUNT(*) FROM {_quote_tabela(tabela_real)}")
    return int(linhas[0][0])


def descrever_tabela(nome_tabela: str) -> Dict[str, Any]:
    """Descrição estruturada: colunas, tipos, nulabilidade, PK e FKs."""
    if _view_logica_ativa(nome_tabela):
        colunas = [{"nome": c["nome"], "tipo": c["tipo"],
                    "permite_nulo": True, "chave_primaria": False}
                   for c in _COLUNAS_VIEW_INFO]
        return {"tabela": config.VIEW_NAME, "colunas": colunas,
                "chave_primaria": [], "relacionamentos": _relacionamentos_logicos()}
    tabela_real = validar_tabela(nome_tabela)
    insp = _inspector()
    pk = insp.get_pk_constraint(tabela_real, schema=config.DB_SCHEMA).get("constrained_columns", [])
    fks = insp.get_foreign_keys(tabela_real, schema=config.DB_SCHEMA)
    colunas = []
    for c in insp.get_columns(tabela_real, schema=config.DB_SCHEMA):
        colunas.append(
            {
                "nome": c["name"],
                "tipo": str(c["type"]),
                "permite_nulo": bool(c.get("nullable", True)),
                "chave_primaria": c["name"] in pk,
            }
        )
    return {
        "tabela": tabela_real,
        "colunas": colunas,
        "chave_primaria": pk,
        "relacionamentos": [
            {
                "colunas": fk.get("constrained_columns"),
                "referencia_tabela": fk.get("referred_table"),
                "referencia_colunas": fk.get("referred_columns"),
            }
            for fk in fks
        ],
    }


def mostrar_amostra(nome_tabela: str, limite: int = 5) -> Dict[str, Any]:
    limite = _sanitizar_limite(limite, padrao=5, maximo=50)
    if _view_logica_ativa(nome_tabela):
        fonte = _fonte_escopo()
    else:
        fonte = _quote_tabela(validar_tabela(nome_tabela))
    colunas, linhas = _run_select(f"SELECT * FROM {fonte} LIMIT :lim", {"lim": limite})
    return {"colunas": colunas, "linhas": [dict(zip(colunas, l)) for l in linhas]}


def identificar_chave_primaria(nome_tabela: str) -> List[str]:
    if _view_logica_ativa(nome_tabela):
        return []  # views não possuem chave primária
    tabela_real = validar_tabela(nome_tabela)
    return _inspector().get_pk_constraint(tabela_real, schema=config.DB_SCHEMA).get(
        "constrained_columns", []
    )


def identificar_relacionamentos(nome_tabela: str) -> List[Dict[str, Any]]:
    if _view_logica_ativa(nome_tabela):
        return _relacionamentos_logicos()
    tabela_real = validar_tabela(nome_tabela)
    fks = _inspector().get_foreign_keys(tabela_real, schema=config.DB_SCHEMA)
    return [
        {
            "colunas": fk.get("constrained_columns"),
            "referencia_tabela": fk.get("referred_table"),
            "referencia_colunas": fk.get("referred_columns"),
        }
        for fk in fks
    ]


def _sanitizar_limite(limite: Any, padrao: int = 10, maximo: int = 100) -> int:
    try:
        v = int(limite)
    except (TypeError, ValueError):
        return padrao
    return max(1, min(v, maximo))


# ---------------------------------------------------------------------------
# Escopo educacional: descoberta do plano de junção e construção da VIEW
# ---------------------------------------------------------------------------
@dataclass
class PlanoJuncao:
    """Plano de junção descoberto por introspecção (chaves reais, sem inventar)."""

    fato: str
    matricula: Optional[str]
    municipio: Optional[str]
    uf: Optional[str]
    regiao: Optional[str]
    # pares de junção (coluna_esquerda, coluna_direita)
    fato_municipio: Optional[Tuple[str, str]] = None      # fato.co_municipio = municipio.pk
    municipio_uf: Optional[Tuple[str, str]] = None        # municipio.cd_uf = uf.pk
    uf_regiao: Optional[Tuple[str, str]] = None           # uf.cd_regiao = regiao.pk
    matricula_fato: Optional[List[Tuple[str, str]]] = None  # matricula <-> fato (1:1)
    nome_municipio: Optional[str] = None
    nome_uf: Optional[str] = None
    nome_regiao: Optional[str] = None
    avisos: List[str] = field(default_factory=list)


def _achar_fk(insp, tabela: str, alvo_lower: str) -> Optional[Dict[str, Any]]:
    """Retorna a primeira FK de `tabela` que referencia a tabela `alvo`."""
    for fk in insp.get_foreign_keys(tabela, schema=config.DB_SCHEMA):
        if (fk.get("referred_table") or "").lower() == alvo_lower:
            return fk
    return None


def _achar_coluna_nome(cols_lower: Dict[str, str], entidade: str) -> Optional[str]:
    """Heurística para achar a coluna descritiva (nome) de uma dimensão."""
    # 1) padrão nome_<entidade> / no_<entidade>
    for padrao in (f"nome_{entidade}", f"no_{entidade}", f"nm_{entidade}", f"ds_{entidade}"):
        if padrao in cols_lower:
            return cols_lower[padrao]
    # 2) qualquer coluna que comece com nome_/no_/nm_/desc
    for lower, real in cols_lower.items():
        if re.match(r"^(nome_|no_|nm_|desc|ds_)", lower):
            return real
    return None


def identificar_chaves_relacionamento() -> PlanoJuncao:
    """Descobre, por introspecção, as chaves que ligam as tabelas do escopo.

    Ordem de resolução para cada ligação: (1) chave estrangeira declarada;
    (2) override do .env; (3) heurística de nomes. Se nada resolver, registra
    aviso para que o usuário informe a coluna correta (não inventa chaves).
    """
    insp = _inspector()
    reais = _tabelas_reais()
    ov = config.OVERRIDES

    def real_ou_none(nome: str) -> Optional[str]:
        return reais.get(nome.lower())

    fato = real_ou_none(config.TABELA_FATO)
    if not fato:
        raise RelacionamentoError(
            f"Tabela principal '{config.TABELA_FATO}' não encontrada no banco."
        )
    matricula = real_ou_none(config.TABELA_MATRICULA)
    municipio = real_ou_none(config.TABELA_MUNICIPIO)
    uf = real_ou_none(config.TABELA_UF)
    regiao = real_ou_none(config.TABELA_REGIAO)

    plano = PlanoJuncao(fato=fato, matricula=matricula, municipio=municipio, uf=uf, regiao=regiao)

    # Ligação fato -> município
    if municipio:
        pk_mun = insp.get_pk_constraint(municipio, schema=config.DB_SCHEMA).get(
            "constrained_columns", []
        )
        fk = _achar_fk(insp, fato, municipio.lower())
        if fk and fk.get("constrained_columns") and fk.get("referred_columns"):
            plano.fato_municipio = (fk["constrained_columns"][0], fk["referred_columns"][0])
        elif ov.fk_municipio and (ov.pk_municipio or pk_mun):
            plano.fato_municipio = (ov.fk_municipio, ov.pk_municipio or pk_mun[0])
        else:
            plano.avisos.append(
                "Não foi possível identificar automaticamente a ligação "
                "inep_censo_escolar -> municipio. Defina FK_MUNICIPIO/PK_MUNICIPIO no .env."
            )
        mun_cols = _colunas_reais(municipio)
        plano.nome_municipio = ov.nome_municipio or _achar_coluna_nome(mun_cols, "municipio")

        # Ligação município -> UF
        if uf:
            pk_uf = insp.get_pk_constraint(uf, schema=config.DB_SCHEMA).get(
                "constrained_columns", []
            )
            fk_uf = _achar_fk(insp, municipio, uf.lower())
            if fk_uf and fk_uf.get("constrained_columns") and fk_uf.get("referred_columns"):
                plano.municipio_uf = (fk_uf["constrained_columns"][0], fk_uf["referred_columns"][0])
            elif ov.fk_municipio_uf and (ov.pk_uf or pk_uf):
                plano.municipio_uf = (ov.fk_municipio_uf, ov.pk_uf or pk_uf[0])
            else:
                plano.avisos.append(
                    "Não foi possível identificar a ligação municipio -> unidade_federacao."
                )
            uf_cols = _colunas_reais(uf)
            plano.nome_uf = ov.nome_uf or _achar_coluna_nome(uf_cols, "uf")

            # Ligação UF -> região
            if regiao:
                pk_reg = insp.get_pk_constraint(regiao, schema=config.DB_SCHEMA).get(
                    "constrained_columns", []
                )
                fk_reg = _achar_fk(insp, uf, regiao.lower())
                if fk_reg and fk_reg.get("constrained_columns") and fk_reg.get("referred_columns"):
                    plano.uf_regiao = (fk_reg["constrained_columns"][0], fk_reg["referred_columns"][0])
                elif ov.fk_uf_regiao and (ov.pk_regiao or pk_reg):
                    plano.uf_regiao = (ov.fk_uf_regiao, ov.pk_regiao or pk_reg[0])
                else:
                    plano.avisos.append(
                        "Não foi possível identificar a ligação unidade_federacao -> regiao."
                    )
                reg_cols = _colunas_reais(regiao)
                plano.nome_regiao = ov.nome_regiao or _achar_coluna_nome(reg_cols, "regiao")

    # Ligação matrícula -> fato (1:1)
    if matricula:
        fk_mat = _achar_fk(insp, matricula, fato.lower())
        if fk_mat and fk_mat.get("constrained_columns") and fk_mat.get("referred_columns"):
            plano.matricula_fato = list(
                zip(fk_mat["constrained_columns"], fk_mat["referred_columns"])
            )
        else:
            # Heurística: chaves comuns nu_ano_censo / co_entidade
            mat_cols = _colunas_reais(matricula)
            fato_cols = _colunas_reais(fato)
            pares = []
            for c in ("nu_ano_censo", "co_entidade"):
                if c in mat_cols and c in fato_cols:
                    pares.append((mat_cols[c], fato_cols[c]))
            if pares:
                plano.matricula_fato = pares
            else:
                plano.avisos.append(
                    "Não foi possível identificar a ligação matrícula -> inep_censo_escolar."
                )

    return plano


def inspecionar_tabelas_escopo() -> Dict[str, Any]:
    """Retorna a estrutura (colunas, PK, FKs) das tabelas do escopo + plano de junção."""
    estrutura = {}
    for tabela in config.TABELAS_ESCOPO:
        try:
            estrutura[tabela] = descrever_tabela(tabela)
        except BancoError as exc:
            estrutura[tabela] = {"erro": str(exc)}
    plano = identificar_chaves_relacionamento()
    return {"tabelas": estrutura, "plano_juncao": plano}


# ---- Construção do SELECT consolidado (base da VIEW) -----------------------
# Estado de inicialização do escopo (preenchido por inicializar_escopo()).
_PLANO: Optional[PlanoJuncao] = None
_SCOPE_SELECT: Optional[str] = None
_COLUNAS_VIEW: List[str] = []                 # nomes canônicos das colunas da VIEW
_COLUNAS_VIEW_INFO: List[Dict[str, str]] = []  # [{"nome", "tipo"}] da VIEW
_VIEW_FISICA: bool = False                    # True se a VIEW física existir no banco


def _eh_alvo_view(nome: Optional[str]) -> bool:
    """True se o nome informado se refere à VIEW consolidada do escopo."""
    return bool(nome) and nome.strip().lower() == config.VIEW_NAME.lower()


def _tipos_reais(nome_tabela_real: str) -> Dict[str, str]:
    """Mapa {nome_lower: tipo_str} das colunas de uma tabela."""
    insp = _inspector()
    return {c["name"].lower(): str(c["type"])
            for c in insp.get_columns(nome_tabela_real, schema=config.DB_SCHEMA)}


def _construir_select_escopo(plano: PlanoJuncao) -> Tuple[str, List[Dict[str, str]]]:
    """Monta o SELECT consolidado a partir do plano de junção descoberto.

    Só inclui colunas do escopo que realmente existem (não inventa nomes).
    Retorna (sql_select, lista_de_colunas com nome e tipo).
    """
    fato_cols = _colunas_reais(plano.fato)
    mat_cols = _colunas_reais(plano.matricula) if plano.matricula else {}
    fato_tipos = _tipos_reais(plano.fato)
    mat_tipos = _tipos_reais(plano.matricula) if plano.matricula else {}

    selects: List[str] = []
    colunas_info: List[Dict[str, str]] = []

    # Colunas descritivas das dimensões.
    if plano.municipio and plano.nome_municipio:
        selects.append(f'm.{_quote(plano.nome_municipio)} AS {_quote("nome_municipio")}')
        colunas_info.append({"nome": "nome_municipio",
                             "tipo": _tipos_reais(plano.municipio).get(plano.nome_municipio.lower(), "texto")})
    if plano.uf:
        uf_cols = _colunas_reais(plano.uf)
        uf_tipos = _tipos_reais(plano.uf)
        if "sigla_uf" in uf_cols:
            selects.append(f'u.{_quote(uf_cols["sigla_uf"])} AS {_quote("sigla_uf")}')
            colunas_info.append({"nome": "sigla_uf", "tipo": uf_tipos.get("sigla_uf", "texto")})
        if plano.nome_uf:
            selects.append(f'u.{_quote(plano.nome_uf)} AS {_quote("nome_uf")}')
            colunas_info.append({"nome": "nome_uf", "tipo": uf_tipos.get(plano.nome_uf.lower(), "texto")})
    if plano.regiao and plano.nome_regiao:
        selects.append(f'r.{_quote(plano.nome_regiao)} AS {_quote("nome_regiao")}')
        colunas_info.append({"nome": "nome_regiao",
                             "tipo": _tipos_reais(plano.regiao).get(plano.nome_regiao.lower(), "texto")})

    # Variáveis do escopo: TP_* (na fato) e QT_MAT_* (na matrícula).
    # Usamos os nomes reais em minúsculas (como no banco) para evitar a
    # necessidade de aspas duplas nas consultas (mais robusto para o NL->SQL).
    for var in config.VARIAVEIS_ESCOPO:
        low = var.lower()
        if low in fato_cols:
            selects.append(f'f.{_quote(fato_cols[low])} AS {low}')
            colunas_info.append({"nome": low, "tipo": fato_tipos.get(low, "")})
        elif low in mat_cols:
            selects.append(f'mat.{_quote(mat_cols[low])} AS {low}')
            colunas_info.append({"nome": low, "tipo": mat_tipos.get(low, "")})
        # se não existir em nenhuma das duas, é simplesmente ignorada.

    if not selects:
        raise RelacionamentoError("Nenhuma coluna do escopo foi encontrada nas tabelas.")

    # FROM + JOINs (sempre LEFT JOIN para preservar todas as escolas).
    from_sql = [f"FROM {_quote_tabela(plano.fato)} f"]

    if plano.matricula and plano.matricula_fato:
        cond = " AND ".join(
            f"mat.{_quote(mc)} = f.{_quote(fc)}" for mc, fc in plano.matricula_fato
        )
        from_sql.append(f"LEFT JOIN {_quote_tabela(plano.matricula)} mat ON {cond}")

    if plano.municipio and plano.fato_municipio:
        fc, mc = plano.fato_municipio
        from_sql.append(
            f"LEFT JOIN {_quote_tabela(plano.municipio)} m ON f.{_quote(fc)} = m.{_quote(mc)}"
        )
        if plano.uf and plano.municipio_uf:
            mu, uu = plano.municipio_uf
            from_sql.append(
                f"LEFT JOIN {_quote_tabela(plano.uf)} u ON m.{_quote(mu)} = u.{_quote(uu)}"
            )
            if plano.regiao and plano.uf_regiao:
                ur, rr = plano.uf_regiao
                from_sql.append(
                    f"LEFT JOIN {_quote_tabela(plano.regiao)} r ON u.{_quote(ur)} = r.{_quote(rr)}"
                )

    sql = "SELECT\n  " + ",\n  ".join(selects) + "\n" + "\n".join(from_sql)
    return sql, colunas_info


def criar_ou_validar_view_escopo_ia() -> Dict[str, Any]:
    """Descobre o plano, monta o SELECT e tenta criar a VIEW física.

    Se o usuário do banco for somente-leitura (sem permissão de CREATE), cai para
    o modo "VIEW lógica" (subconsulta equivalente), 100% read-only. Em ambos os
    casos o chatbot funciona normalmente.
    """
    global _PLANO, _SCOPE_SELECT, _COLUNAS_VIEW, _COLUNAS_VIEW_INFO, _VIEW_FISICA

    plano = identificar_chaves_relacionamento()
    select_sql, colunas_info = _construir_select_escopo(plano)
    _PLANO = plano
    _SCOPE_SELECT = select_sql
    _COLUNAS_VIEW_INFO = colunas_info
    _COLUNAS_VIEW = [c["nome"] for c in colunas_info]

    relatorio: Dict[str, Any] = {
        "plano": plano,
        "colunas_view": _COLUNAS_VIEW,
        "select": select_sql,
        "avisos": list(plano.avisos),
    }

    # A VIEW já existe fisicamente?
    if config.VIEW_NAME.lower() in _tabelas_reais():
        _VIEW_FISICA = True
        relatorio["status"] = "view_existente"
        relatorio["modo"] = "view_fisica"
        return relatorio

    # Tenta criar a VIEW física.
    engine = conectar_banco()
    ddl = f"CREATE OR REPLACE VIEW {_quote_tabela(config.VIEW_NAME)} AS\n{select_sql}"
    try:
        with engine.begin() as conn:
            conn.execute(text(ddl))
        _VIEW_FISICA = True
        relatorio["status"] = "view_criada"
        relatorio["modo"] = "view_fisica"
    except SQLAlchemyError as exc:
        # Sem permissão de escrita: usa VIEW lógica (subconsulta).
        _VIEW_FISICA = False
        relatorio["status"] = "view_logica"
        relatorio["modo"] = "view_logica"
        relatorio["motivo"] = (
            "Usuário somente-leitura: a VIEW física não pôde ser criada; o sistema "
            "usa a consulta consolidada equivalente (read-only). "
            f"Detalhe: {str(exc)[:120]}"
        )
    return relatorio


def inicializar_escopo() -> Dict[str, Any]:
    """Garante que o plano/seleção do escopo estejam prontos (idempotente)."""
    if _SCOPE_SELECT is None:
        return criar_ou_validar_view_escopo_ia()
    return {"status": "ja_inicializado", "colunas_view": _COLUNAS_VIEW}


def _fonte_escopo() -> str:
    """Retorna a fonte de dados do escopo: a VIEW física ou a subconsulta lógica."""
    inicializar_escopo()
    if _VIEW_FISICA:
        return _quote_tabela(config.VIEW_NAME)
    return f"(\n{_SCOPE_SELECT}\n) AS vw_escopo_ia"


def _colunas_existentes(nomes: List[str]) -> List[str]:
    """Resolve nomes de colunas para os nomes REAIS existentes na VIEW.

    Aceita nomes em qualquer caixa (ex.: 'QT_MAT_FUND') e devolve o nome real da
    coluna na view (ex.: 'qt_mat_fund'), preservando a ordem e ignorando ausentes.
    """
    inicializar_escopo()
    mapa = {c.lower(): c for c in _COLUNAS_VIEW}
    return [mapa[n.lower()] for n in nomes if n.lower() in mapa]


def _soma_total_matriculas_sql() -> str:
    """Expressão SQL que soma as matrículas das etapas existentes (INF+FUND+MED)."""
    etapas = _colunas_existentes(list(config.COLUNAS_ETAPA.values()))
    if not etapas:
        raise ColunaInvalidaError("Nenhuma coluna de matrícula por etapa disponível na base.")
    partes = [f"COALESCE({_quote(c)}, 0)" for c in etapas]
    return " + ".join(partes)


# ---------------------------------------------------------------------------
# Consultas específicas do escopo educacional
# ---------------------------------------------------------------------------
def listar_variaveis_escopo() -> List[Dict[str, str]]:
    """Lista as variáveis do escopo efetivamente disponíveis na base final."""
    inicializar_escopo()
    return [{"variavel": c} for c in _COLUNAS_VIEW]


def _agrupar_matriculas(coluna_grupo: str, limite: Optional[int] = None,
                        coluna_etapa: Optional[str] = None) -> Dict[str, Any]:
    """Agrega matrículas por uma coluna de grupo (município/UF/região).

    Se coluna_etapa for informada (ex.: QT_MAT_FUND), soma apenas aquela etapa;
    caso contrário soma o total (INF+FUND+MED).
    """
    fonte = _fonte_escopo()
    grupo = _colunas_existentes([coluna_grupo])
    if not grupo:
        raise ColunaInvalidaError(f"A coluna de agrupamento '{coluna_grupo}' não está na base.")
    grupo_q = _quote(grupo[0])

    if coluna_etapa:
        etapa = _colunas_existentes([coluna_etapa])
        if not etapa:
            raise ColunaInvalidaError(f"A coluna '{coluna_etapa}' não está na base.")
        medida = f"SUM(COALESCE({_quote(etapa[0])}, 0))"
    else:
        medida = f"SUM({_soma_total_matriculas_sql()})"

    params: Dict[str, Any] = {}
    sql = (
        f"SELECT {grupo_q} AS grupo, {medida} AS total_matriculas\n"
        f"FROM {fonte}\n"
        f"WHERE {grupo_q} IS NOT NULL\n"
        f"GROUP BY {grupo_q}\n"
        f"ORDER BY total_matriculas DESC NULLS LAST"
    )
    if limite:
        sql += "\nLIMIT :lim"
        params["lim"] = _sanitizar_limite(limite, padrao=10, maximo=100)
    colunas, linhas = _run_select(sql, params)
    return {
        "colunas": ["grupo", "total_matriculas"],
        "linhas": [{"grupo": l[0], "total_matriculas": int(l[1] or 0)} for l in linhas],
    }


def consultar_total_matriculas_por_municipio(limite: Optional[int] = 15) -> Dict[str, Any]:
    return _agrupar_matriculas(config.COL_NOME_MUNICIPIO_VIEW, limite=limite or 15)


def consultar_total_matriculas_por_uf(limite: Optional[int] = None,
                                      coluna_etapa: Optional[str] = None) -> Dict[str, Any]:
    return _agrupar_matriculas(config.COL_NOME_UF_VIEW, limite=limite, coluna_etapa=coluna_etapa)


def consultar_total_matriculas_por_regiao(coluna_etapa: Optional[str] = None) -> Dict[str, Any]:
    return _agrupar_matriculas(config.COL_NOME_REGIAO_VIEW, coluna_etapa=coluna_etapa)


def consultar_matriculas_por_etapa_ensino(coluna: Optional[str] = None) -> Dict[str, Any]:
    """Total de matrículas por etapa de ensino (infantil/fundamental/médio).

    Se `coluna` indicar uma etapa específica, retorna apenas o total dela.
    """
    fonte = _fonte_escopo()
    if coluna:
        etapa = _colunas_existentes([coluna])
        if not etapa:
            raise ColunaInvalidaError(f"A etapa '{coluna}' não está na base.")
        colunas, linhas = _run_select(
            f"SELECT SUM(COALESCE({_quote(etapa[0])},0)) FROM {fonte}"
        )
        return {
            "colunas": ["etapa", "total_matriculas"],
            "linhas": [{"etapa": coluna, "total_matriculas": int(linhas[0][0] or 0)}],
        }

    rotulos = {"QT_MAT_INF": "Educação Infantil", "QT_MAT_FUND": "Ensino Fundamental",
               "QT_MAT_MED": "Ensino Médio"}
    resultado = []
    for canonica, rotulo in rotulos.items():
        existe = _colunas_existentes([canonica])
        if not existe:
            continue
        _, linhas = _run_select(f"SELECT SUM(COALESCE({_quote(existe[0])},0)) FROM {fonte}")
        resultado.append({"etapa": rotulo, "total_matriculas": int(linhas[0][0] or 0)})
    return {"colunas": ["etapa", "total_matriculas"], "linhas": resultado}


def consultar_matriculas_por_genero() -> Dict[str, Any]:
    fonte = _fonte_escopo()
    mapa = {"Feminino": config.COLUNAS_GENERO["feminino"],
            "Masculino": config.COLUNAS_GENERO["masculino"]}
    resultado = []
    for rotulo, canonica in mapa.items():
        existe = _colunas_existentes([canonica])
        if not existe:
            continue
        _, linhas = _run_select(f"SELECT SUM(COALESCE({_quote(existe[0])},0)) FROM {fonte}")
        resultado.append({"genero": rotulo, "total_matriculas": int(linhas[0][0] or 0)})
    return {"colunas": ["genero", "total_matriculas"], "linhas": resultado}


def consultar_matriculas_por_raca_cor() -> Dict[str, Any]:
    fonte = _fonte_escopo()
    rotulos = {
        "QT_MAT_BAS_BRANCA": "Branca", "QT_MAT_BAS_PRETA": "Preta",
        "QT_MAT_BAS_PARDA": "Parda", "QT_MAT_BAS_AMARELA": "Amarela",
        "QT_MAT_BAS_INDIGENA": "Indígena", "QT_MAT_BAS_ND": "Não declarada",
    }
    resultado = []
    for canonica, rotulo in rotulos.items():
        existe = _colunas_existentes([canonica])
        if not existe:
            continue
        _, linhas = _run_select(f"SELECT SUM(COALESCE({_quote(existe[0])},0)) FROM {fonte}")
        resultado.append({"raca_cor": rotulo, "total_matriculas": int(linhas[0][0] or 0)})
    resultado.sort(key=lambda x: x["total_matriculas"], reverse=True)
    return {"colunas": ["raca_cor", "total_matriculas"], "linhas": resultado}


# Rótulos para variáveis categóricas (melhoram a leitura das respostas).
_ROTULOS_TP = {
    "TP_LOCALIZACAO": {1: "Urbana", 2: "Rural"},
    "TP_LOCALIZACAO_DIFERENCIADA": {
        0: "Não está em área diferenciada", 1: "Assentamento", 2: "Terra indígena",
        3: "Área quilombola", 4: "Não está em área diferenciada",
        5: "Assentamento", 6: "Terra indígena", 7: "Área quilombola",
        8: "Área ribeirinha",
    },
    "TP_CATEGORIA_ESCOLA_PRIVADA": {
        1: "Particular", 2: "Comunitária", 3: "Confessional", 4: "Filantrópica",
    },
}


def _rotular_valor(coluna_canonica: str, valor: Any) -> Any:
    """Aplica rótulo amigável a uma variável categórica.

    Se o valor já vier como texto (ex.: 'Urbana'), é mantido. Se vier como código
    numérico e houver um mapa de rótulos, traduz o código.
    """
    if valor is None:
        return "Não informado"
    mapa = _ROTULOS_TP.get(coluna_canonica.upper(), {})
    if mapa:
        try:
            return mapa.get(int(valor), valor)
        except (TypeError, ValueError):
            return valor
    return valor


def consultar_distribuicao_por_localizacao() -> Dict[str, Any]:
    """Distribuição de escolas e matrículas por TP_LOCALIZACAO (Urbana/Rural)."""
    fonte = _fonte_escopo()
    col = _colunas_existentes(["TP_LOCALIZACAO"])
    if not col:
        raise ColunaInvalidaError("A coluna TP_LOCALIZACAO não está na base.")
    col_q = _quote(col[0])
    total_expr = _soma_total_matriculas_sql()
    sql = (
        f"SELECT {col_q} AS cod, COUNT(*) AS escolas, SUM({total_expr}) AS matriculas\n"
        f"FROM {fonte}\nGROUP BY {col_q}\nORDER BY matriculas DESC NULLS LAST"
    )
    colunas, linhas = _run_select(sql)
    out = []
    for cod, escolas, mat in linhas:
        rotulo = _rotular_valor("TP_LOCALIZACAO", cod)
        out.append({"localizacao": rotulo, "escolas": int(escolas or 0),
                    "matriculas": int(mat or 0)})
    return {"colunas": ["localizacao", "escolas", "matriculas"], "linhas": out}


def consultar_valores_distintos(coluna: str, limite: int = 30) -> Dict[str, Any]:
    """Valores distintos de uma coluna do escopo, com contagem de ocorrências."""
    fonte = _fonte_escopo()
    existe = _colunas_existentes([coluna])
    if not existe:
        raise ColunaInvalidaError(
            f"A coluna '{coluna}' não está na base final. "
            f"Colunas disponíveis: {', '.join(_COLUNAS_VIEW)}."
        )
    col_q = _quote(existe[0])
    limite = _sanitizar_limite(limite, padrao=30, maximo=200)
    sql = (
        f"SELECT {col_q} AS valor, COUNT(*) AS ocorrencias\n"
        f"FROM {fonte}\nGROUP BY {col_q}\nORDER BY ocorrencias DESC NULLS LAST\nLIMIT :lim"
    )
    colunas, linhas = _run_select(sql, {"lim": limite})
    # Aplica rótulos amigáveis quando for uma variável categórica conhecida.
    out = [{"valor": _rotular_valor(existe[0], valor), "ocorrencias": int(ocor or 0)}
           for valor, ocor in linhas]
    return {"colunas": ["valor", "ocorrencias"], "linhas": out}


def gerar_resumo_estatistico_escopo() -> Dict[str, Any]:
    """Resumo geral da base final: totais de escolas e de matrículas por recorte."""
    fonte = _fonte_escopo()
    resumo: List[Dict[str, Any]] = []

    _, linhas = _run_select(f"SELECT COUNT(*) FROM {fonte}")
    resumo.append({"indicador": "Total de escolas (registros)", "valor": int(linhas[0][0] or 0)})

    total_expr = _soma_total_matriculas_sql()
    _, linhas = _run_select(f"SELECT SUM({total_expr}) FROM {fonte}")
    resumo.append({"indicador": "Total de matrículas (INF+FUND+MED)",
                   "valor": int(linhas[0][0] or 0)})

    for canonica, rotulo in (("QT_MAT_INF", "Matrículas - Educação Infantil"),
                             ("QT_MAT_FUND", "Matrículas - Ensino Fundamental"),
                             ("QT_MAT_MED", "Matrículas - Ensino Médio")):
        existe = _colunas_existentes([canonica])
        if existe:
            _, l = _run_select(f"SELECT SUM(COALESCE({_quote(existe[0])},0)) FROM {fonte}")
            resumo.append({"indicador": rotulo, "valor": int(l[0][0] or 0)})

    for canonica, rotulo in (("QT_MAT_BAS_FEM", "Matrículas femininas"),
                             ("QT_MAT_BAS_MASC", "Matrículas masculinas")):
        existe = _colunas_existentes([canonica])
        if existe:
            _, l = _run_select(f"SELECT SUM(COALESCE({_quote(existe[0])},0)) FROM {fonte}")
            resumo.append({"indicador": rotulo, "valor": int(l[0][0] or 0)})

    return {"colunas": ["indicador", "valor"], "linhas": resumo}


# ---------------------------------------------------------------------------
# Cobertura do banco inteiro: estatísticas globais e consulta NL->SQL segura
# ---------------------------------------------------------------------------
def _tabelas_escopo_reais() -> List[str]:
    """Nomes reais (existentes) das tabelas-base do escopo."""
    reais = _tabelas_reais()
    return [reais[t.lower()] for t in config.TABELAS_ESCOPO if t.lower() in reais]


def estatisticas_banco() -> Dict[str, Any]:
    """Estatísticas das tabelas do escopo (ou de todo o banco, se habilitado).

    Reporta o número de tabelas e o total de colunas somando essas tabelas.
    """
    insp = _inspector()
    if config.PERMITIR_TODAS_TABELAS:
        tabelas = insp.get_table_names(schema=config.DB_SCHEMA)
        views = insp.get_view_names(schema=config.DB_SCHEMA)
        escopo_txt = "todo o banco"
    else:
        tabelas = _tabelas_escopo_reais()
        views = [config.VIEW_NAME]
        escopo_txt = "as tabelas do escopo"

    total_colunas = 0
    for t in tabelas:
        total_colunas += len(insp.get_columns(t, schema=config.DB_SCHEMA))

    return {
        "num_tabelas": len(tabelas),
        "num_views": len(views),
        "total_colunas": total_colunas,
        "schema": config.DB_SCHEMA,
        "abrangencia": escopo_txt,
        "tabelas": tabelas,
    }


def listar_todas_tabelas() -> List[str]:
    """Tabelas candidatas para a seleção do NL->SQL.

    Por padrão, restringe às tabelas-base do escopo; se PERMITIR_TODAS_TABELAS
    estiver ativo, devolve todas as tabelas/views do schema.
    """
    if not config.PERMITIR_TODAS_TABELAS:
        return _tabelas_escopo_reais()
    insp = _inspector()
    return insp.get_table_names(schema=config.DB_SCHEMA) + insp.get_view_names(schema=config.DB_SCHEMA)


def _assert_tabelas_permitidas(sql: str) -> None:
    """Garante que o SELECT só referencia tabelas do escopo (allowlist).

    Extrai os identificadores após FROM/JOIN e verifica contra a allowlist,
    ignorando subconsultas e nomes de CTE definidos no próprio SQL.
    """
    if config.PERMITIR_TODAS_TABELAS:
        return
    permitidas = {t.lower() for t in config.ALLOWLIST_TABELAS}
    # Nomes de CTE (WITH nome AS (...)) e aliases de subconsulta são permitidos.
    ctes = {m.group(1).lower() for m in re.finditer(r"(\w+)\s+AS\s*\(", sql, re.IGNORECASE)}
    padrao = re.compile(
        r"\b(?:FROM|JOIN)\s+(\"[^\"]+\"(?:\.\"[^\"]+\")?|[a-zA-Z_][\w$]*(?:\.[a-zA-Z_][\w$]*)?)",
        re.IGNORECASE,
    )
    for ref in padrao.findall(sql):
        nome = ref.split(".")[-1].strip().strip('"').lower()
        if nome and nome not in permitidas and nome not in ctes:
            raise TabelaInvalidaError(
                f"A consulta referencia a tabela '{nome}', que está fora do escopo "
                f"do projeto. Tabelas permitidas: {', '.join(config.ALLOWLIST_TABELAS)}."
            )


def descrever_schema_texto(nomes_tabelas: List[str], max_colunas: int = 200) -> str:
    """Gera uma descrição textual (colunas + chaves estrangeiras) para o prompt de SQL.

    Inclui as FKs entre as tabelas para que o modelo gere JOINs corretos. Resolve os
    nomes contra o schema real (case-insensitive) e ignora nomes inexistentes.
    """
    reais = _tabelas_reais()
    insp = _inspector()
    partes: List[str] = []
    relacionamentos: List[str] = []
    for nome in nomes_tabelas:
        real = reais.get((nome or "").strip().lower())
        if not real:
            continue
        cols = insp.get_columns(real, schema=config.DB_SCHEMA)
        desc_cols = [f"{c['name']} [{c['type']}]" for c in cols[:max_colunas]]
        extra = "" if len(cols) <= max_colunas else f" ... (+{len(cols) - max_colunas} colunas)"
        partes.append(f"- {config.DB_SCHEMA}.{real} ({len(cols)} colunas): "
                      + ", ".join(desc_cols) + extra)
        for fk in insp.get_foreign_keys(real, schema=config.DB_SCHEMA):
            cc = ", ".join(fk.get("constrained_columns") or [])
            rt = fk.get("referred_table")
            rc = ", ".join(fk.get("referred_columns") or [])
            if cc and rt:
                relacionamentos.append(f"  {real}({cc}) -> {rt}({rc})")
    texto = "TABELAS E COLUNAS:\n" + "\n".join(partes)
    if relacionamentos:
        texto += "\n\nRELACIONAMENTOS (use exatamente estas chaves nos JOINs):\n" + "\n".join(relacionamentos)
    return texto


def _injetar_limite(sql: str, max_linhas: int) -> str:
    """Garante um LIMIT no SELECT (sem duplicar) para conter o volume de retorno."""
    limpo = sql.strip().rstrip(";").strip()
    if re.search(r"\blimit\b", limpo, re.IGNORECASE):
        return limpo
    return f"{limpo}\nLIMIT {int(max_linhas)}"


def descrever_view_texto() -> str:
    """Descrição da base consolidada vw_escopo_ia (para o NL->SQL do escopo)."""
    inicializar_escopo()
    cols = ", ".join(f"{c['nome']} [{c['tipo']}]" for c in _COLUNAS_VIEW_INFO)
    return (
        f"Existe UMA tabela consolidada chamada vw_escopo_ia (use exatamente este "
        f"nome, sem prefixo de schema e SEM aspas). Ela une escolas, matrículas, "
        f"município, UF e região. Todas as colunas estão em minúsculas:\n{cols}\n\n"
        "Observações: colunas qt_mat_* são quantidades de matrículas (inteiro); "
        "nome_municipio, nome_uf, sigla_uf e nome_regiao são textos; "
        "tp_localizacao já vem como texto ('Urbana'/'Rural'); cada linha é uma escola."
    )


def _envolver_view_logica(sql: str) -> str:
    """Quando a VIEW é lógica, injeta vw_escopo_ia como CTE para a consulta funcionar."""
    inicializar_escopo()
    if _VIEW_FISICA:
        return sql
    cte = f"vw_escopo_ia AS (\n{_SCOPE_SELECT}\n)"
    if re.match(r"^\s*WITH\b", sql, re.IGNORECASE):
        # Já existe um WITH: anexa a CTE da view como primeira definição.
        return re.sub(r"^\s*WITH\b", f"WITH {cte},", sql, count=1, flags=re.IGNORECASE)
    return f"WITH {cte}\n{sql}"


def executar_sql_escopo(sql: str, max_linhas: Optional[int] = None) -> Dict[str, Any]:
    """Executa SQL do escopo, gerado contra vw_escopo_ia (somente leitura).

    Retorna {sql, colunas, linhas}, onde "sql" é a consulta legível (contra a view).
    """
    max_linhas = max_linhas or config.MAX_LINHAS_SQL
    if not sql or not sql.strip():
        raise BancoError("A IA não retornou uma consulta SQL válida.")
    sql_legivel = _injetar_limite(sql, max_linhas)
    _assert_select_only(sql_legivel)
    _assert_tabelas_permitidas(sql_legivel)
    # Para execução, garante que vw_escopo_ia exista (como view física ou CTE).
    sql_exec = _injetar_limite(_envolver_view_logica(sql), max_linhas)
    _assert_select_only(sql_exec)
    colunas, linhas = _run_select(sql_exec)
    return {
        "sql": sql_legivel,
        "colunas": colunas,
        "linhas": [dict(zip(colunas, l)) for l in linhas],
    }


def executar_sql_seguro(sql: str, max_linhas: Optional[int] = None) -> Dict[str, Any]:
    """Valida e executa uma consulta SELECT gerada pela IA (somente leitura).

    Defesas: aceita apenas SELECT/WITH, bloqueia comandos destrutivos, injeta
    LIMIT e executa em transação somente leitura. Retorna {sql, colunas, linhas}.
    """
    max_linhas = max_linhas or config.MAX_LINHAS_SQL
    if not sql or not sql.strip():
        raise BancoError("A IA não retornou uma consulta SQL válida.")
    sql_final = _injetar_limite(sql, max_linhas)
    _assert_select_only(sql_final)        # apenas SELECT, sem comandos destrutivos
    _assert_tabelas_permitidas(sql_final)  # somente tabelas do escopo (allowlist)
    colunas, linhas = _run_select(sql_final)
    return {
        "sql": sql_final,
        "colunas": colunas,
        "linhas": [dict(zip(colunas, l)) for l in linhas],
    }


if __name__ == "__main__":  # diagnóstico rápido da camada de banco
    print("Conectando...", conectar_banco().url)
    rel = criar_ou_validar_view_escopo_ia()
    print("Status da VIEW:", rel["status"], "| modo:", rel["modo"])
    print("Colunas da base final:", rel["colunas_view"])
    if rel.get("avisos"):
        print("Avisos:", rel["avisos"])
    print("Matrículas por região:")
    for linha in consultar_total_matriculas_por_regiao()["linhas"]:
        print("  ", linha)
