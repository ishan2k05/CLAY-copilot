import asyncio
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app, graph_store, ChatRequest, ACTIVE_SESSION_TOKEN
from app.parser.symbols import SymbolNode

client = TestClient(app)
client.headers.update({"X-Clay-Auth-Token": ACTIVE_SESSION_TOKEN})

def test_mentions_endpoint_file_and_folder_search(tmp_path):
    # Setup test workspace files
    test_file = tmp_path / "calc.py"
    test_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    
    sub_dir = tmp_path / "models"
    sub_dir.mkdir()
    user_file = sub_dir / "user.py"
    user_file.write_text("class User:\n    pass\n", encoding="utf-8")

    response = client.get(f"/api/context/mentions?query=calc&workspace_path={tmp_path}")
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    names = [r["name"] for r in data["results"]]
    assert any("calc.py" in n for n in names)

    # Test folder search
    response_folder = client.get(f"/api/context/mentions?query=models&workspace_path={tmp_path}")
    assert response_folder.status_code == 200
    data_folder = response_folder.json()
    assert any(r["type"] == "folder" for r in data_folder["results"])

def test_mentions_endpoint_symbol_search():
    # Insert mock symbol into graph store
    node = SymbolNode(
        id="calc.py:evaluate_expression",
        name="evaluate_expression",
        symbol_type="function",
        file_path="src/calc.py",
        start_line=12,
        end_line=24,
        docstring="Evaluates mathematical tokens."
    )
    asyncio.run(graph_store.insert_batch([node], []))

    response = client.get("/api/context/mentions?query=evaluate")
    assert response.status_code == 200
    data = response.json()
    symbols = [r for r in data["results"] if r.get("type") == "symbol"]
    assert any(s["name"] == "evaluate_expression" for s in symbols)

def test_chat_request_accepts_pinned_mentions():
    req = ChatRequest(
        query="Refactor this function",
        pinned_mentions=[
            {"type": "file", "name": "calc.py", "path": "src/calc.py"},
            {"type": "symbol", "name": "evaluate_expression", "path": "src/calc.py", "kind": "function", "line": 12}
        ]
    )
    assert req.pinned_mentions is not None
    assert len(req.pinned_mentions) == 2
    assert req.pinned_mentions[0]["name"] == "calc.py"
    assert req.pinned_mentions[1]["name"] == "evaluate_expression"
