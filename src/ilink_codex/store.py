from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: Path, chunk_chars: int = 1000, max_reply_chars: int = 12000):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.chunk_chars = chunk_chars
        self.max_reply_chars = max_reply_chars
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS heartbeat(role TEXT PRIMARY KEY, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors(account TEXT PRIMARY KEY, cursor TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen(account TEXT, message TEXT, PRIMARY KEY(account,message));
            CREATE TABLE IF NOT EXISTS peers(
                account TEXT, user TEXT, context TEXT NOT NULL, session TEXT,
                PRIMARY KEY(account,user));
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY, account TEXT NOT NULL, user TEXT NOT NULL,
                prompt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                cancel INTEGER NOT NULL DEFAULT 0, result TEXT, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(state,id);
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY, account TEXT NOT NULL, user TEXT NOT NULL,
                body TEXT NOT NULL, client_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0);
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def reply(self, account: str, user: str, text: str):
        if len(text) > self.max_reply_chars:
            text = text[:self.max_reply_chars] + "\n[回复已截断；完整结果保存在服务器任务数据库中。]"
        chunks = [text[i:i + self.chunk_chars] for i in range(0, len(text), self.chunk_chars)]
        for i, chunk in enumerate(chunks):
            prefix = f"[{i+1}/{len(chunks)}] " if len(chunks) > 1 else ""
            self.db.execute("INSERT INTO outbox(account,user,body,client_id) VALUES(?,?,?,?)",
                            (account, user, prefix + chunk, f"ilink-codex-{uuid.uuid4()}"))

    def cursor(self, account: str) -> str:
        row = self.db.execute("SELECT cursor FROM cursors WHERE account=?", (account,)).fetchone()
        return row[0] if row else ""

    def set_cursor(self, account: str, cursor: str):
        self.db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(account) DO UPDATE SET cursor=excluded.cursor",
                        (account, cursor))

    def receive(self, account: str, msg: dict, config):
        """Called only after authorization and inside the poll batch transaction."""
        user = msg.get("from_user_id")
        message_id = msg.get("message_id")
        context = msg.get("context_token")
        if (not isinstance(user, str) or not isinstance(context, str) or not context
                or message_id is None or isinstance(message_id, (dict, list, bool))
                or msg.get("message_type") != 1 or msg.get("message_state", 2) != 2):
            return
        inserted = self.db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (account, str(message_id)))
        if not inserted.rowcount:
            return
        self.db.execute("""INSERT INTO peers(account,user,context) VALUES(?,?,?)
            ON CONFLICT(account,user) DO UPDATE SET context=excluded.context""", (account, user, context))
        items = msg.get("item_list", [])
        if not isinstance(items, list):
            return
        text = "\n".join(i.get("text_item", {}).get("text", "") for i in items
                         if isinstance(i, dict) and i.get("type") == 1
                         and isinstance(i.get("text_item"), dict)
                         and isinstance(i["text_item"].get("text"), str)).strip()
        if not text:
            self.reply(account, user, "第一版仅支持文字消息。")
            return
        if len(text) > config.max_prompt_chars:
            self.reply(account, user, "消息过长，请缩短后重试。")
            return
        pending = self.db.execute("SELECT id,state FROM tasks WHERE account=? AND user=? AND state IN ('queued','running') ORDER BY id", (account, user)).fetchall()
        if text == "/status":
            rows = ", ".join(f"#{r['id']} {r['state']}" for r in pending)
            self.reply(account, user, rows or "目前没有待执行任务。")
        elif text == "/cancel":
            self.db.execute("UPDATE tasks SET cancel=1 WHERE account=? AND user=? AND state='running'", (account, user))
            self.db.execute("UPDATE tasks SET state='cancelled',result='执行前取消' WHERE account=? AND user=? AND state='queued'", (account, user))
            self.reply(account, user, "已取消排队任务，并请求停止当前任务；已产生的文件修改不会回滚。")
        elif text == "/new":
            if pending:
                self.reply(account, user, "请等待任务结束，或先 /cancel，再用 /new 重置对话。")
            else:
                self.db.execute("UPDATE peers SET session=NULL WHERE account=? AND user=?", (account, user))
                self.reply(account, user, "下条消息将开启新会话；工作文件保留。")
        elif text == "/result":
            row = self.db.execute("SELECT id,result FROM tasks WHERE account=? AND user=? AND result IS NOT NULL ORDER BY id DESC LIMIT 1", (account, user)).fetchone()
            self.reply(account, user, f"任务 #{row['id']}\n{row['result']}" if row else "还没有任务结果。")
        elif text == "/help" or text.startswith("/"):
            self.reply(account, user, "直接发送文字开始任务。\n/status 状态\n/cancel 停止当前及排队任务\n/new 新对话\n/result 最近结果\n/help 帮助")
        elif len(pending) >= config.max_pending_per_user:
            self.reply(account, user, "你的任务队列已满，请稍后再试。")
        else:
            cur = self.db.execute("INSERT INTO tasks(account,user,prompt,created) VALUES(?,?,?,?)", (account, user, text, time.time()))
            self.reply(account, user, f"已收到，任务 #{cur.lastrowid} 已排队。")

    def claim(self):
        with self.transaction():
            row = self.db.execute("""SELECT t.* FROM tasks t WHERE t.state='queued'
                AND NOT EXISTS(SELECT 1 FROM tasks r WHERE r.account=t.account AND r.user=t.user AND r.state='running')
                ORDER BY t.id LIMIT 1""").fetchone()
            if row:
                self.db.execute("UPDATE tasks SET state='running' WHERE id=?", (row['id'],))
                return dict(row)

    def peer(self, account: str, user: str):
        return self.db.execute("SELECT * FROM peers WHERE account=? AND user=?", (account, user)).fetchone()

    def cancelled(self, task_id: int) -> bool:
        return bool(self.db.execute("SELECT cancel FROM tasks WHERE id=?", (task_id,)).fetchone()[0])

    def finish(self, task: dict, state: str, result: str, session: str | None = None):
        with self.transaction():
            self.db.execute("UPDATE tasks SET state=?,result=? WHERE id=?", (state, result, task['id']))
            if session:
                self.db.execute("UPDATE peers SET session=? WHERE account=? AND user=?", (session, task['account'], task['user']))
            self.reply(task['account'], task['user'], f"任务 #{task['id']} [{state}]\n{result}")

    def recover(self):
        for row in self.db.execute("SELECT * FROM tasks WHERE state='running'").fetchall():
            self.finish(dict(row), 'interrupted', '服务中断，任务不会自动重跑；可能已有文件修改。请核实后重新发起任务。')

    def next_reply(self, account: str):
        return self.db.execute("""SELECT o.*,p.context FROM outbox o JOIN peers p
            ON o.account=p.account AND o.user=p.user
            WHERE o.account=? AND o.state='pending' AND o.next_at<=?
            AND NOT EXISTS(SELECT 1 FROM outbox prior WHERE prior.account=o.account
                AND prior.user=o.user AND prior.state='pending' AND prior.id<o.id)
            ORDER BY o.id LIMIT 1""", (account, time.time())).fetchone()

    def sent(self, reply_id: int):
        self.db.execute("UPDATE outbox SET state='sent' WHERE id=?", (reply_id,))

    def send_failed(self, row, limit: int):
        attempts = row['attempts'] + 1
        self.db.execute("UPDATE outbox SET attempts=?,state=?,next_at=? WHERE id=?",
                        (attempts, 'failed' if attempts >= limit else 'pending',
                         time.time() + min(300, 2 ** attempts), row['id']))

    def close(self):
        self.db.close()

    def beat(self, role: str):
        self.db.execute('INSERT INTO heartbeat VALUES(?,?) ON CONFLICT(role) DO UPDATE SET updated=excluded.updated',
                        (role, time.time()))
