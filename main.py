import functions_framework
import hashlib
import json
import logging
import os
import io
import fitz
from datetime import datetime, timezone
from openai import AzureOpenAI
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration, SearchField, SearchFieldDataType,
    SearchIndex, SimpleField, SearchableField, VectorSearch, VectorSearchProfile
)
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential
from azure.storage.blob import BlobServiceClient
import traceback
        
# ── Configurações ──────────────────────────────────────────────────────────
OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
CHAT_DEPLOYMENT = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o")
EMBEDDING_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
EMBED_ENDPOINT = os.environ.get("AZURE_OPENAI_EMBED_ENDPOINT", OPENAI_ENDPOINT)
EMBED_KEY = os.environ.get("AZURE_OPENAI_EMBED_KEY", OPENAI_KEY)
SEARCH_ENDPOINT = os.environ.get("AZURE_SEARCH_ENDPOINT")
SEARCH_KEY = os.environ.get("AZURE_SEARCH_KEY")
INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX_NAME", "rag-culinaria-index")
STORAGE_CONN = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
TOP_N = int(os.environ.get("RAG_TOP_N", "5"))
EMBEDDING_DIM = 1536

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def _load_prompt(filename):
    with open(os.path.join(PROMPTS_DIR, filename), "r", encoding="utf-8") as f:
        return f.read()


def _get_chat_client():
    return AzureOpenAI(
        azure_endpoint=OPENAI_ENDPOINT,
        api_key=OPENAI_KEY,
        api_version=OPENAI_API_VERSION,
    )


def _get_embed_client():
    return AzureOpenAI(
        azure_endpoint=EMBED_ENDPOINT,
        api_key=EMBED_KEY,
        api_version=os.environ.get("AZURE_OPENAI_EMBED_API_VERSION", "2024-02-01"),
    )


def _get_blob_service():
    return BlobServiceClient.from_connection_string(STORAGE_CONN)


def _read_blob(container, blob_name):
    service = _get_blob_service()
    blob_client = service.get_blob_client(container=container, blob=blob_name)
    return blob_client.download_blob().readall()


def _write_json_blob(container, blob_name, data):
    service = _get_blob_service()
    container_client = service.get_container_client(container)
    if not container_client.exists():
        container_client.create_container()
    blob_client = service.get_blob_client(container=container, blob=blob_name)
    blob_client.upload_blob(
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        overwrite=True
    )


def _read_text(file_bytes):
    if file_bytes[:4] == b'%PDF':
        text_parts = []
        with fitz.open(stream=io.BytesIO(file_bytes), filetype="pdf") as doc:
            for i, page in enumerate(doc, 1):
                t = page.get_text("text")
                if t.strip():
                    text_parts.append(f"[Pagina {i}]\n{t}")
        return "\n\n".join(text_parts)
    try:
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return file_bytes.decode("latin-1")


def _get_embeddings(texts):
    client = _get_embed_client()
    response = client.embeddings.create(
        model=EMBEDDING_DEPLOYMENT,
        input=texts,
    )
    return [item.embedding for item in response.data]


def _ensure_index():
    index_client = SearchIndexClient(
        endpoint=SEARCH_ENDPOINT,
        credential=AzureKeyCredential(SEARCH_KEY),
    )
    existing = [idx.name for idx in index_client.list_indexes()]
    if INDEX_NAME in existing:
        return INDEX_NAME
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="chunk_id", type=SearchFieldDataType.Int32, filterable=True),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SimpleField(name="source_document", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="page_number", type=SearchFieldDataType.Int32, filterable=True),
        SimpleField(name="timestamp", type=SearchFieldDataType.String),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIM,
            vector_search_profile_name="hnsw-profile",
        ),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="hnsw-algo")],
        profiles=[VectorSearchProfile(name="hnsw-profile", algorithm_configuration_name="hnsw-algo")],
    )
    index_client.create_index(SearchIndex(name=INDEX_NAME, fields=fields, vector_search=vector_search))
    return INDEX_NAME


def _hybrid_search(query_text, query_vector, top_n=5):
    client = SearchClient(
        endpoint=SEARCH_ENDPOINT,
        index_name=INDEX_NAME,
        credential=AzureKeyCredential(SEARCH_KEY),
    )
    vector_query = VectorizedQuery(
        vector=query_vector,
        k_nearest_neighbors=top_n,
        fields="content_vector",
    )
    results = client.search(
        search_text=query_text,
        vector_queries=[vector_query],
        select=["id", "chunk_id", "title", "content", "source_document", "page_number"],
        top=top_n,
    )
    return [
        {
            "id": r["id"],
            "chunk_id": r.get("chunk_id"),
            "title": r.get("title", ""),
            "content": r.get("content", ""),
            "source_document": r.get("source_document", ""),
            "score": r["@search.score"],
        }
        for r in results
    ]


# ── Endpoint: POST /process ────────────────────────────────────────────────
@functions_framework.http
def process(request):
    if request.method == "GET" and request.path.endswith("/health"):
        return json.dumps({"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}), 200, {"Content-Type": "application/json"}

    if request.method != "POST":
        return json.dumps({"error": "Method not allowed"}), 405, {"Content-Type": "application/json"}

    try:
        body = request.get_json()
        blob_name = (body.get("blob_name") or "").strip()
        container = (body.get("container") or "documents").strip()
    except Exception:
        return json.dumps({"error": "Body JSON invalido"}), 400, {"Content-Type": "application/json"}

    if not blob_name:
        return json.dumps({"error": "blob_name obrigatorio"}), 400, {"Content-Type": "application/json"}

    try:
        # 1. Ler arquivo
        file_bytes = _read_blob(container, blob_name)

        # 2. Extrair texto
        document_text = _read_text(file_bytes)
        if not document_text.strip():
            return json.dumps({"error": "Documento vazio"}), 422, {"Content-Type": "application/json"}

        # 3. Chunking via LLM
        system_prompt = _load_prompt("chunking_system.md")
        client = _get_chat_client()
        response = client.chat.completions.create(
            model=CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Processe o documento:\n\n{document_text[:12000]}"},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content)
        chunks = parsed.get("chunks", parsed) if isinstance(parsed, dict) else parsed

        # 4. Metadados
        timestamp = datetime.now(timezone.utc).isoformat()
        doc_base = os.path.splitext(blob_name)[0]
        for i, chunk in enumerate(chunks):
            chunk["source_document"] = blob_name
            chunk["timestamp"] = timestamp
            if "chunk_id" not in chunk:
                chunk["chunk_id"] = i + 1

        # 5. Gravar chunks
        _write_json_blob("chunks", f"{doc_base}/chunks.json", chunks)

        # 6. Índice + embeddings
        _ensure_index()
        contents = [c.get("content", "") for c in chunks]
        embeddings = []
        for i in range(0, len(contents), 16):
            embeddings.extend(_get_embeddings(contents[i:i+16]))

        # 7. Indexar
        documents = []
        for i, chunk in enumerate(chunks):
            doc_id = hashlib.md5(f"{blob_name}_{chunk.get('chunk_id', i+1)}".encode()).hexdigest()
            documents.append({
                "id": doc_id,
                "chunk_id": int(chunk.get("chunk_id", i+1)),
                "title": chunk.get("title", f"Chunk {i+1}"),
                "content": chunk.get("content", ""),
                "source_document": blob_name,
                "page_number": int(chunk.get("page_number", 0)),
                "timestamp": timestamp,
                "content_vector": embeddings[i],
            })

        search_client = SearchClient(
            endpoint=SEARCH_ENDPOINT,
            index_name=INDEX_NAME,
            credential=AzureKeyCredential(SEARCH_KEY),
        )
        search_client.upload_documents(documents=documents)

        return json.dumps({
            "status": "ok",
            "blob_name": blob_name,
            "total_chunks": len(chunks),
            "indexed": True,
        }, ensure_ascii=False), 200, {"Content-Type": "application/json"}

    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERRO DETALHADO: {error_detail}")
        return json.dumps({"error": str(e), "detail": error_detail}), 500, {"Content-Type": "application/json"}


# ── Endpoint: POST /query ──────────────────────────────────────────────────
@functions_framework.http
def query(request):
    if request.method != "POST":
        return json.dumps({"error": "Method not allowed"}), 405, {"Content-Type": "application/json"}

    try:
        body = request.get_json()
        question = (body.get("question") or "").strip()
    except Exception:
        return json.dumps({"error": "Body JSON invalido"}), 400, {"Content-Type": "application/json"}

    if not question:
        return json.dumps({"error": "question obrigatorio"}), 400, {"Content-Type": "application/json"}

    try:
        # 1. Embedding da pergunta
        q_embedding = _get_embeddings([question])[0]

        # 2. Busca híbrida
        results = _hybrid_search(question, q_embedding, TOP_N)

        if not results:
            return json.dumps({
                "answer": "Nenhum documento indexado. Execute /process primeiro.",
                "confidence": "low",
                "sources": [],
            }), 200, {"Content-Type": "application/json"}

        # 3. Contexto
        context = "\n\n---\n\n".join([
            f"[CHUNK {i+1} | {r.get('source_document')} | {r.get('title')}]\n{r.get('content')}"
            for i, r in enumerate(results)
        ])

        # 4. LLM
        system_prompt = _load_prompt("rag_system.md")
        client = _get_chat_client()
        response = client.chat.completions.create(
            model=CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"## Contexto:\n\n{context}\n\n---\n\n## Pergunta:\n{question}"},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        result = json.loads(response.choices[0].message.content)
        result["sources"] = [
            {
                "chunk": r["content"][:500],
                "source": r.get("source_document", ""),
                "score": round(r["score"], 4),
                "title": r.get("title", ""),
            }
            for r in results
        ]
        result["chunks_found"] = len(results)

        return json.dumps(result, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"}

    except Exception as e:
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}