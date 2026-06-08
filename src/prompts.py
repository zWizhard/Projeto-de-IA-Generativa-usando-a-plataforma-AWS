"""
prompts.py
==========
Armazena os prompts enviados ao Amazon Bedrock.

Decisão de projeto / segurança: o modelo atua APENAS como classificador de
intenções. Ele nunca gera SQL, nunca executa comandos e nunca explica a
resposta — apenas devolve um JSON com a ação identificada. Toda a execução de
consultas é responsabilidade do código Python (database.py), o que impede que a
IA dispare comandos destrutivos no banco.
"""
from __future__ import annotations

from typing import List

# Ações que o Bedrock pode retornar. Mantida em sincronia com chatbot.py.
ACOES_PERMITIDAS: List[str] = [
    # Metadados / introspecção do banco
    "listar_tabelas",
    "contar_colunas",
    "listar_colunas",
    "contar_registros",
    "descrever_tabela",
    "mostrar_amostra",
    "identificar_chave_primaria",
    "identificar_relacionamentos",
    # Escopo educacional (Censo Escolar / INEP)
    "listar_variaveis_escopo",
    "total_matriculas_por_municipio",
    "total_matriculas_por_uf",
    "total_matriculas_por_regiao",
    "matriculas_por_etapa_ensino",
    "matriculas_por_genero",
    "matriculas_por_raca_cor",
    "distribuicao_por_localizacao",
    "valores_distintos",
    "resumo_estatistico_escopo",
    # Banco inteiro (metadados globais e consulta livre)
    "estatisticas_banco",
    "consulta_sql",
    # Fallback
    "pergunta_nao_suportada",
]


def build_system_prompt(view_name: str, variaveis_escopo: List[str]) -> str:
    """Monta o prompt de sistema para o classificador de intenções.

    O prompt deixa explícito o formato JSON obrigatório, a lista de ações
    permitidas e a regra de que termos como "base", "dados", "censo escolar",
    "INEP" e "matrículas" se referem à VIEW consolidada.
    """
    acoes = "\n".join(f"  - {a}" for a in ACOES_PERMITIDAS)
    variaveis = ", ".join(variaveis_escopo)
    return f"""Você é um classificador de intenções para um chatbot acadêmico conectado a um \
banco de dados educacional do INEP/Censo Escolar.

Sua ÚNICA função é interpretar a pergunta do usuário e retornar um JSON válido.
NÃO gere SQL. NÃO explique a resposta. NÃO execute comandos. Escolha apenas UMA
ação da lista permitida e preencha os campos necessários.

Quando o usuário falar sobre "a base", "os dados", "o censo escolar", "INEP",
"matrículas" ou "base final", considere que ele se refere à VIEW consolidada
chamada "{view_name}" (junção das tabelas inep_censo_escolar, Municipio,
unidade_federacao e regiao).

AÇÕES PERMITIDAS (use exatamente um destes valores no campo "acao"):
{acoes}

Variáveis disponíveis na base final: {variaveis}.

REGRAS DE PREENCHIMENTO:
- "tabela": use "{view_name}" para perguntas sobre a base educacional. Para
  perguntas de metadados sobre outra tabela específica, use o nome citado.
- "coluna": preencha quando a pergunta mencionar uma variável específica
  (ex.: QT_MAT_FUND para ensino fundamental). Caso contrário, use null.
- "filtro": texto curto descrevendo um recorte (ex.: "regiao=Sudeste"), ou null.
- "limite": número inteiro quando o usuário pedir "top N" / "os primeiros N",
  caso contrário null.

MAPEAMENTO DE INTENÇÕES (exemplos de gatilhos):
- "quais tabelas existem" -> listar_tabelas
- "quantas colunas tem a tabela X" -> contar_colunas
- "quais as colunas / campos da base" -> listar_colunas
- "quantos registros / linhas tem a tabela X" -> contar_registros
- "descreva a tabela X / tipos de dados" -> descrever_tabela
- "mostre uma amostra / exemplos de linhas" -> mostrar_amostra
- "qual a chave primária da tabela X" -> identificar_chave_primaria
- "como as tabelas se relacionam / chaves estrangeiras" -> identificar_relacionamentos
- "quais variáveis foram priorizadas" -> listar_variaveis_escopo
- "matrículas por município" -> total_matriculas_por_municipio
- "matrículas por UF / estado / qual UF tem mais matrículas" -> total_matriculas_por_uf
- "matrículas por região / qual região tem mais matrículas" -> total_matriculas_por_regiao
- "matrículas no ensino infantil/fundamental/médio" -> matriculas_por_etapa_ensino
  (preencha "coluna" com QT_MAT_INF, QT_MAT_FUND ou QT_MAT_MED quando for uma etapa específica)
- "matrículas femininas / masculinas / por gênero / sexo" -> matriculas_por_genero
- "matrículas por raça / cor" -> matriculas_por_raca_cor
- "distribuição por localização / urbana / rural" -> distribuicao_por_localizacao
- "valores distintos / quais valores existem na coluna X" -> valores_distintos
  (preencha "coluna" com a coluna citada)
- "resumo estatístico / estatísticas da base / médias e totais" -> resumo_estatistico_escopo
- "quantas tabelas/colunas/variáveis existem no banco TODO" -> estatisticas_banco
- QUALQUER outra pergunta sobre DADOS do banco que não se encaixe nas ações acima
  (outras tabelas, cruzamentos, filtros específicos, rankings, etc.) -> consulta_sql
  (coloque a pergunta original, sem alterações, no campo "filtro")

REGRA IMPORTANTE: só use "pergunta_nao_suportada" quando a pergunta NÃO tiver
relação com o banco de dados (ex.: previsão do tempo, piadas). Se a pergunta for
sobre o banco/dados mas não couber em uma ação específica, use "consulta_sql".

FORMATO OBRIGATÓRIO DE SAÍDA (responda SOMENTE com o JSON, sem texto extra):
{{
  "acao": "...",
  "tabela": "...",
  "coluna": "...",
  "filtro": "...",
  "limite": null
}}

Se a pergunta não for compreendida ou estiver fora do escopo, retorne:
{{
  "acao": "pergunta_nao_suportada",
  "tabela": null,
  "coluna": null,
  "filtro": null,
  "limite": null
}}
"""


# Exemplos few-shot que ajudam modelos abertos (LLaMA) a estabilizar a saída JSON.
FEW_SHOT_EXEMPLOS = [
    (
        "Quantas matrículas existem no ensino fundamental por UF?",
        '{"acao": "total_matriculas_por_uf", "tabela": "vw_escopo_ia", '
        '"coluna": "QT_MAT_FUND", "filtro": null, "limite": null}',
    ),
    (
        "Quais são as colunas da base final?",
        '{"acao": "listar_colunas", "tabela": "vw_escopo_ia", '
        '"coluna": null, "filtro": null, "limite": null}',
    ),
    (
        "Qual a quantidade de matrículas femininas e masculinas?",
        '{"acao": "matriculas_por_genero", "tabela": "vw_escopo_ia", '
        '"coluna": null, "filtro": null, "limite": null}',
    ),
    (
        "Quais tabelas existem no banco?",
        '{"acao": "listar_tabelas", "tabela": null, '
        '"coluna": null, "filtro": null, "limite": null}',
    ),
    (
        "Somando todas as tabelas do escopo, quantas colunas existem?",
        '{"acao": "estatisticas_banco", "tabela": null, '
        '"coluna": null, "filtro": null, "limite": null}',
    ),
    (
        "Quantas escolas rurais existem em cada região?",
        '{"acao": "consulta_sql", "tabela": null, "coluna": null, '
        '"filtro": "Quantas escolas rurais existem em cada região?", "limite": null}',
    ),
    (
        "Qual a média de matrículas do ensino médio por município em Goiás?",
        '{"acao": "consulta_sql", "tabela": null, "coluna": null, '
        '"filtro": "Qual a média de matrículas do ensino médio por município em Goiás?", "limite": null}',
    ),
    (
        "Qual a previsão do tempo amanhã?",
        '{"acao": "pergunta_nao_suportada", "tabela": null, '
        '"coluna": null, "filtro": null, "limite": null}',
    ),
]


# ---------------------------------------------------------------------------
# Prompts do modo NL->SQL (consulta livre ao banco inteiro)
# ---------------------------------------------------------------------------
def build_table_selection_prompt(pergunta: str, tabelas: List[str]) -> str:
    """Prompt para o modelo escolher as tabelas relevantes à pergunta."""
    lista = "\n".join(f"  - {t}" for t in tabelas)
    return f"""Dada a pergunta do usuário e a lista de tabelas de um banco PostgreSQL,
selecione APENAS as tabelas necessárias para responder (no máximo 4).
Responda SOMENTE com um array JSON de nomes de tabela, sem texto extra.

Tabelas disponíveis:
{lista}

Pergunta: {pergunta}

Exemplo de resposta: ["municipio", "pib_municipios"]
Resposta:"""


SQL_SYSTEM_PROMPT = """Você é um gerador de SQL para um banco de dados PostgreSQL.
Regras OBRIGATÓRIAS:
- Gere APENAS uma consulta SELECT (somente leitura). NUNCA use INSERT, UPDATE,
  DELETE, DROP, ALTER, TRUNCATE, CREATE ou qualquer comando que altere dados.
- Use somente as tabelas e colunas fornecidas no esquema. Não invente nomes.
- Use os nomes de tabela EXATAMENTE como aparecem no esquema fornecido.
- Use aspas duplas em colunas com letras maiúsculas (ex.: "QT_MAT_MED").
- Prefira agregações claras (SUM, COUNT, AVG, GROUP BY) e ORDER BY quando o
  usuário pedir "mais", "maior", "ranking", "top".
- Sempre limite o resultado com LIMIT (no máximo {max_linhas}).
- Responda SOMENTE com a consulta SQL, sem explicações e sem blocos de markdown.
"""


def build_sql_generation_prompt(pergunta: str, schema_texto: str, max_linhas: int) -> str:
    """Prompt (conteúdo do usuário) para geração da consulta SQL."""
    return f"""Esquema das tabelas relevantes:
{schema_texto}

Pergunta do usuário: {pergunta}

Gere a consulta SQL (SELECT) que responde à pergunta, com LIMIT {max_linhas}.
SQL:"""


def build_resumo_prompt(pergunta: str, tabela_texto: str) -> str:
    """Prompt para resumir o resultado da consulta em linguagem natural."""
    return f"""Pergunta do usuário: {pergunta}

Resultado da consulta (formato tabular):
{tabela_texto}

Escreva UMA resposta curta, clara e objetiva em português, em no máximo 3 frases,
respondendo à pergunta com base nos dados acima. Não invente números."""


def build_user_prompt(pergunta: str) -> str:
    """Monta o conteúdo do usuário com few-shot + a pergunta atual."""
    linhas = ["Classifique as perguntas a seguir.\n"]
    for pergunta_ex, json_ex in FEW_SHOT_EXEMPLOS:
        linhas.append(f"Pergunta: {pergunta_ex}\nJSON: {json_ex}\n")
    linhas.append(f"Pergunta: {pergunta}\nJSON:")
    return "\n".join(linhas)
