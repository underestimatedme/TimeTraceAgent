"""Local preview server for the autonomous progress experience."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


HOST = "127.0.0.1"
PORT = 8080
APP_ROOT = Path(__file__).resolve().parent
DATABASE_PATH = Path("/tmp/autonomous_progress_todos.sqlite3")
MAX_REQUEST_SIZE = 16 * 1024


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()
    return connection


def serialize_task(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "completed": bool(row["completed"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def list_tasks() -> list[dict[str, object]]:
    with closing(connect_database()) as connection:
        rows = connection.execute(
            "SELECT id, title, completed, created_at, updated_at FROM tasks ORDER BY id DESC"
        ).fetchall()
    return [serialize_task(row) for row in rows]


def create_task(title: str) -> dict[str, object]:
    with closing(connect_database()) as connection:
        with connection:
            cursor = connection.execute("INSERT INTO tasks (title) VALUES (?)", (title,))
            row = connection.execute(
                "SELECT id, title, completed, created_at, updated_at FROM tasks WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("created task could not be read")
    return serialize_task(row)


def update_task(task_id: int, completed: bool) -> dict[str, object] | None:
    with closing(connect_database()) as connection:
        with connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET completed = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(completed), task_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT id, title, completed, created_at, updated_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
    return serialize_task(row) if row is not None else None


class ExperienceHandler(SimpleHTTPRequestHandler):
    """Serve the page assets and its small, local JSON API."""

    server_version = "AutonomousExperience/2.0"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(APP_ROOT), **kwargs)

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, object]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("请求缺少内容长度")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ValueError("请求长度无效") from error
        if length <= 0 or length > MAX_REQUEST_SIZE:
            raise ValueError("请求内容为空或过大")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("请求内容不是有效的 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return payload

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_json({"status": "ok"})
            return
        if path == "/api/tasks":
            try:
                tasks = list_tasks()
            except sqlite3.Error:
                self.log_error("task database read failed")
                self.send_json({"error": "暂时无法读取任务，请稍后重试"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_json({"tasks": tasks})
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/api/tasks":
            try:
                payload = self.read_json()
                title = payload.get("title")
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("请输入任务标题")
                title = title.strip()
                if len(title) > 120:
                    raise ValueError("任务标题不能超过 120 个字符")
                task = create_task(title)
            except ValueError as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            except sqlite3.Error:
                self.log_error("task database create failed")
                self.send_json({"error": "暂时无法新增任务，请稍后重试"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_json({"task": task}, HTTPStatus.CREATED)
            return
        self.send_json({"error": "未找到该接口"}, HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        prefix = "/api/tasks/"
        if not path.startswith(prefix):
            self.send_json({"error": "未找到该接口"}, HTTPStatus.NOT_FOUND)
            return

        try:
            task_id = int(path.removeprefix(prefix))
            if task_id <= 0:
                raise ValueError
        except ValueError:
            self.send_json({"error": "任务编号无效"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            payload = self.read_json()
            completed = payload.get("completed")
            if not isinstance(completed, bool):
                raise ValueError("completed 必须是布尔值")
            task = update_task(task_id, completed)
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except sqlite3.Error:
            self.log_error("task database update failed")
            self.send_json({"error": "暂时无法更新任务，请稍后重试"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if task is None:
            self.send_json({"error": "任务不存在或已被移除"}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"task": task})


def main() -> None:
    with closing(connect_database()):
        pass
    with ThreadingHTTPServer((HOST, PORT), ExperienceHandler) as server:
        print(f"自主推进体验已在 http://{HOST}:{PORT} 启动")
        server.serve_forever()


if __name__ == "__main__":
    main()
