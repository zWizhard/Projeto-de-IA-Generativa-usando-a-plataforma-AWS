"""
app.py
======
Interface web do chatbot (Streamlit).

Contém: título, descrição, campo de pergunta, botão de envio, área de resposta,
histórico da conversa, e seções informativas (tabelas utilizadas, variáveis
priorizadas e exemplos de perguntas).

Execução:
    streamlit run app.py
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import config
import chatbot
import database


st.set_page_config(page_title="Projeto de IAGEN — Chatbot INEP/Censo Escolar",
                   page_icon="🎓", layout="wide")

EXEMPLOS = [
    "Quais tabelas existem no banco?",
    "Quais são as colunas da base final?",
    "Quantas colunas tem a base?",
    "Quantos registros existem na base?",
    "Qual é a chave primária da tabela inep_censo_escolar?",
    "Como as tabelas se relacionam?",
    "Quais variáveis foram priorizadas?",
    "Qual UF possui mais matrículas?",
    "Qual região possui mais matrículas?",
    "Quantas matrículas existem no ensino fundamental por UF?",
    "Matrículas por etapa de ensino",
    "Qual a quantidade de matrículas femininas e masculinas?",
    "Qual a quantidade de matrículas por raça/cor?",
    "Distribuição por localização (urbana/rural)",
    "Resumo estatístico da base",
    # Perguntas livres (NL->SQL) sobre as tabelas do escopo:
    "Quantas escolas rurais existem em cada região?",
    "Qual a média de matrículas do ensino médio por município em Goiás?",
    "Qual a soma de matrículas indígenas por UF?",
    "Quantas escolas urbanas e rurais existem na Bahia?",
]


@st.cache_resource(show_spinner="Conectando ao banco e preparando a base final...")
def _inicializar():
    """Conecta ao banco e prepara o plano da VIEW uma única vez por sessão."""
    return chatbot.inicializar()


def _render_resposta(resp: dict):
    if resp.get("erro"):
        st.error(resp["texto"])
    else:
        st.markdown(resp["texto"])
    tabela = resp.get("tabela")
    if tabela and tabela.get("linhas"):
        linhas = tabela["linhas"]
        if isinstance(linhas[0], dict):
            df = pd.DataFrame(linhas)
        else:
            df = pd.DataFrame(linhas, columns=tabela.get("colunas"))
        st.dataframe(df, use_container_width=True, hide_index=True)
    intencao = resp.get("intencao")
    if intencao:
        sql_gerado = intencao.get("_sql_gerado")
        if sql_gerado:
            with st.expander("🧮 Consulta SQL gerada (somente leitura)"):
                st.code(sql_gerado, language="sql")
        with st.expander("🔎 Intenção identificada pelo Bedrock (JSON)"):
            # Oculta chaves internas (prefixo _) no JSON exibido.
            publico = {k: v for k, v in intencao.items() if not k.startswith("_")}
            st.json(publico)


# ---------------------------------------------------------------------------
# Barra lateral: informações do escopo
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("ℹ️ Sobre o projeto")
    st.write(
        "**Projeto de IAGEN.** Chatbot acadêmico que responde, em linguagem natural, "
        "**qualquer pergunta sobre as tabelas do escopo** do INEP/Censo Escolar, usando "
        "**Amazon Bedrock** para interpretar a pergunta e **consultas SQL seguras "
        "(somente leitura)** para buscar os dados. Perguntas fora dessas tabelas são "
        "recusadas."
    )

    st.subheader("📚 Tabelas utilizadas")
    st.markdown(
        "- `inep_censo_escolar` (principal)\n"
        "- `inep_censo_escolar_matricula` (matrículas)\n"
        "- `municipio` (dimensão)\n"
        "- `unidade_federacao` (dimensão)\n"
        "- `regiao` (dimensão)\n\n"
        f"Base final consolidada: **`{config.VIEW_NAME}`**"
    )

    st.subheader("🎯 Variáveis priorizadas")
    st.caption(", ".join(config.VARIAVEIS_ESCOPO))

    st.subheader("🔐 Segurança")
    st.caption(
        "A IA apenas classifica a intenção (retorna JSON). Ela não gera nem executa "
        "SQL. As consultas são somente leitura, com allowlist de tabelas e "
        "identificadores validados."
    )

    st.subheader("☁️ Bedrock")
    st.caption(f"Região: {config.AWS_REGION} · Modelo: {config.BEDROCK_MODEL_ID}")


# ---------------------------------------------------------------------------
# Página principal
# ---------------------------------------------------------------------------
st.title("🎓 Projeto de IAGEN — Chatbot INEP / Censo Escolar")
st.caption(
    "Faça **qualquer** pergunta em linguagem natural sobre as tabelas do escolar "
    "(escopo: Censo Escolar + município/UF/região). O Amazon Bedrock interpreta a "
    "pergunta e o sistema responde com consultas SQL seguras (somente leitura). "
    "Ex.: *Qual a média de matrículas do ensino médio por município em Goiás?*"
)

# Inicialização (com tratamento de erro amigável).
status_init = None
try:
    status_init = _inicializar()
except Exception as exc:  # noqa: BLE001
    st.error(
        "Não foi possível inicializar a conexão com o banco/escopo. "
        f"Verifique o arquivo .env. Detalhe: {exc}"
    )

if status_init:
    modo = status_init.get("modo") or status_init.get("status")
    if modo == "view_logica":
        st.info(
            "Base final em modo **VIEW lógica** (usuário somente leitura): as "
            "consultas usam a junção consolidada equivalente, 100% read-only.",
            icon="🛡️",
        )
    elif modo in ("view_fisica", "view_criada", "view_existente"):
        st.success(f"VIEW `{config.VIEW_NAME}` disponível no banco.", icon="✅")
    if status_init.get("avisos"):
        for aviso in status_init["avisos"]:
            st.warning(aviso)

# Histórico da conversa.
if "historico" not in st.session_state:
    st.session_state.historico = []

# Exemplos de perguntas (clicáveis).
with st.expander("💡 Exemplos de perguntas que você pode fazer", expanded=False):
    cols = st.columns(3)
    for i, ex in enumerate(EXEMPLOS):
        if cols[i % 3].button(ex, key=f"ex_{i}", use_container_width=True):
            st.session_state.pergunta_pendente = ex

# Campo de entrada + botão.
with st.form("form_pergunta", clear_on_submit=True):
    pergunta = st.text_input(
        "Sua pergunta:",
        value=st.session_state.pop("pergunta_pendente", ""),
        placeholder="Ex.: Quantas matrículas existem no ensino médio por região?",
    )
    enviar = st.form_submit_button("Enviar", type="primary")

if enviar and pergunta.strip():
    with st.spinner("Pensando..."):
        resposta = chatbot.processar_pergunta(pergunta)
    st.session_state.historico.append({"pergunta": pergunta, "resposta": resposta})

# Renderiza o histórico (mais recente primeiro).
if st.session_state.historico:
    st.subheader("💬 Conversa")
    for item in reversed(st.session_state.historico):
        with st.chat_message("user"):
            st.markdown(item["pergunta"])
        with st.chat_message("assistant"):
            _render_resposta(item["resposta"])

if st.session_state.historico:
    if st.button("🧹 Limpar histórico"):
        st.session_state.historico = []
        st.rerun()
