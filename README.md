# 🍳 RAG Culinária Brasileira

Trabalho Final da disciplina **Cloud & Cognitive Environments** — FIAP MBA em Data Science & IA.

## 📋 Descrição

Sistema de **Retrieval-Augmented Generation (RAG)** especializado em culinária brasileira. O sistema indexa documentos com receitas e responde perguntas com base no conteúdo indexado, usando busca vetorial semântica e geração de texto por LLM.

## 🏗️ Arquitetura

```
Cliente (Postman)
      │
      ▼
Google Cloud Functions
  ├── POST /process  → Chunking + Indexação
  └── POST /query    → Busca + Resposta RAG
      │
      ├── Azure Blob Storage (documentos + chunks)
      ├── Azure OpenAI GPT-4o (chat)
      ├── Azure OpenAI text-embedding-3-small (embeddings)
      └── Azure AI Search (índice vetorial)
```

## ☁️ Infraestrutura

| Serviço | Provedor | Recurso |
|---|---|---|
| Functions API | Google Cloud | Cloud Functions (Python 3.11) |
| Blob Storage | Azure | ragculinariasto |
| AI Search | Azure | rag-culinaria-search |
| OpenAI GPT-4o | Azure | mr362-movuf1vx-eastus2 |
| OpenAI Embeddings | Azure | rag-culinaria-openai |

## 🚀 Endpoints

### POST /process
Processa e indexa um documento do Blob Storage.

**URL:** `https://us-central1-rag-culinaria.cloudfunctions.net/process`

**Body:**
```json
{
  "container": "documents",
  "blob_name": "receitas_brasileiras.txt"
}
```

**Resposta:**
```json
{
  "status": "ok",
  "blob_name": "receitas_brasileiras.txt",
  "total_chunks": 7,
  "indexed": true
}
```

### POST /query
Consulta RAG com resposta gerada por LLM.

**URL:** `https://us-central1-rag-culinaria.cloudfunctions.net/query`

**Body:**
```json
{
  "question": "Quais são os ingredientes para fazer pão de queijo?"
}
```

**Resposta:**
```json
{
  "answer": "Os ingredientes para fazer pão de queijo são...",
  "confidence": "high",
  "found_in_context": true,
  "sources": [...]
}
```

## 📁 Estrutura do Projeto

```
rag-culinaria-brasileira/
├── main.py              # Cloud Functions: process + query
├── requirements.txt     # Dependências Python
└── prompts/
    ├── chunking_system.md   # Prompt de chunking semântico
    └── rag_system.md        # Prompt RAG
```

## 🎬 Vídeo de Demonstração

> Link do vídeo: [a ser adicionado]

## 🔧 Decisões Técnicas

- **Chunking semântico via LLM:** GPT-4o identifica fronteiras semânticas naturais (uma receita = um chunk)
- **Busca híbrida:** vetorial (semântica) + BM25 (texto) para melhores resultados
- **text-embedding-3-small:** melhor custo-benefício para embeddings de 1536 dimensões
- **Google Cloud Functions:** escolhido por suporte nativo a Python 3.11 e plano gratuito
