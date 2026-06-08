"""
setup_view.py
=============
Script de linha de comando que:
  1. Conecta ao banco de dados.
  2. Inspeciona a estrutura real das tabelas do escopo (não inventa chaves).
  3. Descobre o plano de junção a partir das chaves estrangeiras declaradas.
  4. Cria a VIEW vw_escopo_ia (ou usa o modo "VIEW lógica" se for somente leitura).

Uso:
    python setup_view.py

É seguro rodar várias vezes (idempotente).
"""
from __future__ import annotations

import sys

import config
import database

# Garante saída UTF-8 no console (evita UnicodeEncodeError no Windows/cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _linha(c: str = "-", n: int = 70) -> None:
    print(c * n)


def main() -> None:
    print("Conectando ao banco de dados...")
    try:
        engine = database.conectar_banco()
    except database.BancoIndisponivelError as exc:
        print(f"[ERRO] {exc}")
        return
    print(f"Conectado: {engine.url}\n")

    _linha("=")
    print("INSPEÇÃO DAS TABELAS DO ESCOPO")
    _linha("=")
    try:
        info = database.inspecionar_tabelas_escopo()
    except database.BancoError as exc:
        print(f"[ERRO] {exc}")
        return

    for tabela, desc in info["tabelas"].items():
        if "erro" in desc:
            print(f"\n- {tabela}: {desc['erro']}")
            continue
        pk = ", ".join(desc["chave_primaria"]) or "(nenhuma)"
        print(f"\n- {tabela}: {len(desc['colunas'])} colunas | PK: {pk}")
        for rel in desc["relacionamentos"]:
            print(
                f"    FK {rel['colunas']} -> "
                f"{rel['referencia_tabela']}({rel['referencia_colunas']})"
            )

    plano = info["plano_juncao"]
    print("\n")
    _linha("=")
    print("PLANO DE JUNÇÃO DESCOBERTO")
    _linha("=")
    print(f"Fato:            {plano.fato}")
    print(f"Matrícula:       {plano.matricula}")
    print(f"Município:       {plano.municipio} (nome: {plano.nome_municipio})")
    print(f"UF:              {plano.uf} (nome: {plano.nome_uf})")
    print(f"Região:          {plano.regiao} (nome: {plano.nome_regiao})")
    print(f"fato <-> matricula:   {plano.matricula_fato}")
    print(f"fato <-> municipio:   {plano.fato_municipio}")
    print(f"municipio <-> uf:     {plano.municipio_uf}")
    print(f"uf <-> regiao:        {plano.uf_regiao}")
    if plano.avisos:
        print("\nAVISOS (informe os nomes corretos no .env, se necessário):")
        for a in plano.avisos:
            print(f"  - {a}")

    print("\n")
    _linha("=")
    print(f"CRIAÇÃO/VALIDAÇÃO DA VIEW {config.VIEW_NAME}")
    _linha("=")
    rel = database.criar_ou_validar_view_escopo_ia()
    print(f"Status: {rel['status']} | Modo: {rel['modo']}")
    if rel.get("motivo"):
        print(f"Motivo: {rel['motivo']}")
    print(f"\nColunas da base final ({len(rel['colunas_view'])}):")
    print("  " + ", ".join(rel["colunas_view"]))
    print("\nSELECT consolidado (base da VIEW):\n")
    print(rel["select"])

    print("\n")
    _linha("=")
    print("VALIDAÇÃO RÁPIDA (matrículas por região)")
    _linha("=")
    try:
        for linha in database.consultar_total_matriculas_por_regiao()["linhas"]:
            print(f"  {linha['grupo']}: {linha['total_matriculas']:,}".replace(",", "."))
    except database.BancoError as exc:
        print(f"[ERRO] {exc}")

    print("\nConcluído.")


if __name__ == "__main__":
    main()
