# 🎓 Projeto de IAGEN — Chatbot INEP / Censo Escolar com Amazon Bedrock

Chatbot acadêmico que responde, em **linguagem natural**, **qualquer pergunta sobre
as tabelas do escopo** da base educacional do **INEP/Censo Escolar**. O **Amazon
Bedrock** interpreta a pergunta e o **código Python** executa **consultas SQL seguras
(somente leitura)** sobre um banco **PostgreSQL** — inclusive gerando consultas
dinâmicas (NL→SQL) para perguntas livres, sempre restritas às tabelas do escopo.

> Trabalho desenvolvido a partir do escopo de tabelas e variáveis definido pelo
> docente da disciplina.

---

## 1. Descrição

O usuário digita perguntas como *"Qual UF possui mais matrículas?"*. O fluxo é:

1. A pergunta vai ao **Amazon Bedrock**, que **classifica a intenção** e devolve um
   JSON controlado (ex.: `{"acao": "total_matriculas_por_uf", ...}`).
2. O Python interpreta o JSON e chama a **função segura** correspondente.
3. A função executa um **SELECT validado** sobre a base consolidada e retorna os dados.
4. O chatbot devolve uma **resposta clara** (texto + tabela).

A IA **nunca** gera nem executa SQL — ela só interpreta a pergunta.

## 2. Tecnologias

- **Python 3.10+**
- **Streamlit** — interface web
- **Amazon Bedrock** (`boto3`) — IA generativa (modelo LLaMA 4 Scout)
- **PostgreSQL** + **SQLAlchemy** + **psycopg2** — banco de dados
- **pandas** — exibição de tabelas
- **python-dotenv** — variáveis de ambiente

## 3. Estrutura de pastas

```
projeto_IA/
├── app.py               # Interface Streamlit
├── bedrock_client.py    # Cliente do Amazon Bedrock (SSO + interpretação de intenção)
├── database.py          # Conexão, introspecção, VIEW e consultas seguras
├── chatbot.py           # Orquestração pergunta -> intenção -> consulta -> resposta
├── prompts.py           # Prompt de sistema e few-shot do classificador
├── config.py            # Carrega variáveis de ambiente e o escopo do projeto
├── setup_view.py        # CLI: inspeciona o banco e cria/valida a VIEW vw_escopo_ia
├── requirements.txt
├── README.md
├── .env.example         # Modelo de configuração (sem credenciais)
├── .gitignore
├── relatorio/           # Relatório técnico em LaTeX
│   ├── main.tex
│   └── referencias.bib
└── tests/
    └── test_database.py
```

## 4. Configuração do ambiente

```bash
# 1. (Opcional) crie um ambiente virtual
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# 2. Instale as dependências
pip install -r requirements.txt
```

> **Máquina sem permissão de administrador (ex.: laboratório acadêmico):**
> instale as dependências no perfil do usuário, sem precisar de admin:
> ```bash
> python -m pip install --user -r requirements.txt
> ```
> E execute sempre com `python -m streamlit run app.py` (veja a seção 7).

## 5. Configuração do `.env`

Copie o modelo e preencha:

```bash
copy .env.example .env   # Windows
cp .env.example .env     # Linux/Mac
```

Campos principais:

```env
AWS_REGION=us-east-2
BEDROCK_MODEL_ID=us.meta.llama4-scout-17b-instruct-v1:0

DB_ENGINE=postgresql
DB_HOST=bigdata.dataiesb.com
DB_PORT=5432
DB_NAME=iesb
DB_USER=data_iesb
DB_PASSWORD=********
DB_SCHEMA=public
```

> A detecção dos relacionamentos é automática (via chaves estrangeiras). Se o seu
> banco não declarar as FKs, informe-as manualmente nas variáveis `FK_*`/`PK_*`
> do `.env` (veja `.env.example`).

## 6. Autenticação no Amazon Bedrock (SSO)

O acesso usa **IAM Identity Center** (login no navegador). Na primeira execução o
navegador abre para login; as credenciais ficam em cache (~8h) em
`~/.bedrock_creds.json`.

```bash
# Teste rápido do Bedrock (script original do curso):
python test_bedrock.py "O que é computação em nuvem?"

# Teste do classificador de intenção do chatbot:
python bedrock_client.py "Qual UF tem mais matrículas?"
```

## 7. Como executar

```bash
# 1. (Recomendado) inspecione o banco e crie/valide a VIEW
python setup_view.py

# 2. Suba a interface (use sempre "python -m streamlit")
python -m streamlit run app.py
```

> **No Windows, com duplo clique:** basta executar o arquivo `executar.bat`.
>
> **Importante:** use `python -m streamlit run app.py` (e não apenas
> `streamlit run app.py`). O atalho `streamlit` pode não estar no PATH do
> sistema, resultando em *"'streamlit' não é reconhecido como comando"*. Rodar
> via `python -m streamlit` evita esse problema.

Também é possível usar o chatbot pelo terminal:

```bash
python chatbot.py "Quantas matrículas existem no ensino fundamental por UF?"
```

## 8. Exemplos de perguntas

**Metadados**
- Quais tabelas existem no banco?
- Quais são as colunas da base final?
- Quantos registros existem na base?
- Qual é a chave primária da tabela `inep_censo_escolar`?
- Como as tabelas se relacionam?

**Escopo educacional**
- Qual UF possui mais matrículas?
- Qual região possui mais matrículas?
- Quantas matrículas existem no ensino fundamental por UF?
- Matrículas por etapa de ensino.
- Qual a quantidade de matrículas femininas e masculinas?
- Qual a quantidade de matrículas por raça/cor?
- Distribuição por localização (urbana/rural).
- Resumo estatístico da base.

## 9. Observações sobre segurança

- A IA **apenas interpreta a intenção** (retorna JSON); **não gera nem executa SQL**.
- Consultas **somente leitura** (`SET TRANSACTION READ ONLY`) e guarda que bloqueia
  `DROP/DELETE/UPDATE/INSERT/ALTER/TRUNCATE/GRANT/REVOKE/CREATE`.
- **Allowlist** de tabelas do escopo; nomes de tabela/coluna **validados** contra o
  schema real e **citados** (quoting) — evitando injeção de SQL.
- Credenciais ficam **somente no `.env`** (fora do controle de versão).

## 10. Observações sobre o Amazon Bedrock

- Região configurável (`AWS_REGION`, padrão `us-east-2`); o login SSO ocorre em
  `us-east-1` (onde está o Identity Center).
- Modelo padrão: `us.meta.llama4-scout-17b-instruct-v1:0` (perfil de inferência
  multi-região). É possível trocar o modelo em `BEDROCK_MODEL_ID`.
- O cliente usa a **Converse API** (com *fallback* para `invoke_model` no formato
  LLaMA) e renova credenciais automaticamente quando expiram.

## 11. Observações sobre o escopo do professor

- **Tabelas:** `inep_censo_escolar` (principal), `municipio`, `unidade_federacao`,
  `regiao`. A inspeção do banco mostrou que as variáveis de matrícula (`QT_MAT_*`)
  estão na tabela-detalhe **`inep_censo_escolar_matricula`** (relação 1:1 com a
  fato por `nu_ano_censo` + `co_entidade`), por isso ela também é unida na VIEW.
- **Variáveis:** `TP_CATEGORIA_ESCOLA_PRIVADA`, `TP_LOCALIZACAO`,
  `TP_LOCALIZACAO_DIFERENCIADA`, `QT_MAT_INF`, `QT_MAT_FUND`, `QT_MAT_MED`,
  `QT_MAT_BAS_FEM/MASC/ND/BRANCA/PRETA/PARDA/AMARELA/INDIGENA`.
- **VIEW `vw_escopo_ia`:** consolida nome do município/UF/região + as variáveis.
  Como o usuário do banco é somente leitura, o sistema usa por padrão uma
  **VIEW lógica** (subconsulta equivalente, read-only); se houver permissão de
  escrita, a VIEW física é criada automaticamente.
