"""
tests/test_database.py
======================
Testes da camada de banco de dados.

- Testes de SEGURANÇA (guarda de SQL e validação) rodam offline, sem banco.
- Testes de INTEGRAÇÃO conectam ao banco real e são pulados automaticamente
  quando o banco não está acessível (ex.: sem rede / sem .env).

Execute com:
    pytest -q
"""
from __future__ import annotations

import pytest

import config
import database


# ---------------------------------------------------------------------------
# Testes de segurança (offline)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE alunos",
        "DELETE FROM inep_censo_escolar",
        "UPDATE municipio SET nome_municipio = 'x'",
        "INSERT INTO regiao VALUES (1, 'x')",
        "TRUNCATE inep_censo_escolar",
        "ALTER TABLE municipio ADD COLUMN x int",
        "GRANT ALL ON municipio TO public",
    ],
)
def test_bloqueia_sql_destrutivo(sql):
    with pytest.raises(database.BancoError):
        database._assert_select_only(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM vw_escopo_ia",
    "  WITH t AS (SELECT 1) SELECT * FROM t",
    "(SELECT 1) ",
])
def test_permite_select(sql):
    # Não deve levantar exceção.
    database._assert_select_only(sql)


def test_sanitizar_limite():
    assert database._sanitizar_limite("10") == 10
    assert database._sanitizar_limite(-5, padrao=10) == 1
    assert database._sanitizar_limite(99999, maximo=100) == 100
    assert database._sanitizar_limite("abc", padrao=7) == 7


def test_allowlist_configurada():
    assert config.VIEW_NAME in config.ALLOWLIST_TABELAS
    assert config.TABELA_FATO in config.ALLOWLIST_TABELAS
    assert len(config.VARIAVEIS_ESCOPO) == 14


def test_injetar_limite():
    assert "LIMIT" in database._injetar_limite("SELECT * FROM vw_escopo_ia", 50).upper()
    # Não duplica LIMIT existente.
    sql = "SELECT * FROM vw_escopo_ia LIMIT 5"
    assert database._injetar_limite(sql, 50).upper().count("LIMIT") == 1


def test_bloqueia_tabela_fora_escopo(monkeypatch):
    # Com o modo escopo ativo, SQL referenciando outra tabela deve ser bloqueado.
    monkeypatch.setattr(config, "PERMITIR_TODAS_TABELAS", False)
    with pytest.raises(database.TabelaInvalidaError):
        database._assert_tabelas_permitidas("SELECT * FROM pib_municipios")
    # Tabela do escopo é permitida.
    database._assert_tabelas_permitidas("SELECT * FROM vw_escopo_ia")
    # CTE definida no próprio SQL é permitida.
    database._assert_tabelas_permitidas(
        "WITH t AS (SELECT 1) SELECT * FROM t"
    )


# ---------------------------------------------------------------------------
# Testes de integração (requerem banco acessível)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def banco():
    try:
        database.conectar_banco()
        database.inicializar_escopo()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Banco indisponível para teste de integração: {exc}")
    return True


def test_listar_tabelas(banco):
    nomes = {t["nome"].lower() for t in database.listar_tabelas()}
    assert config.TABELA_FATO.lower() in nomes


def test_validar_tabela_fora_escopo(banco):
    with pytest.raises(database.TabelaInvalidaError):
        database.validar_tabela("tabela_que_nao_existe_123")


def test_colunas_view(banco):
    # Os nomes reais das colunas da view são minúsculos (como no banco).
    variaveis = {v["variavel"].lower() for v in database.listar_variaveis_escopo()}
    for v in ("qt_mat_inf", "qt_mat_fund", "qt_mat_med", "nome_uf", "nome_regiao"):
        assert v in variaveis


def test_matriculas_por_regiao(banco):
    res = database.consultar_total_matriculas_por_regiao()
    assert res["linhas"]
    assert all("total_matriculas" in l for l in res["linhas"])
    # A maior região deve ter mais matrículas que a menor.
    totais = [l["total_matriculas"] for l in res["linhas"]]
    assert totais == sorted(totais, reverse=True)


def test_matriculas_por_genero(banco):
    res = database.consultar_matriculas_por_genero()
    rotulos = {l["genero"] for l in res["linhas"]}
    assert {"Feminino", "Masculino"} <= rotulos
