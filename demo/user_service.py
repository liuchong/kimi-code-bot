"""Demo service with intentional bugs for kimi-bot e2e testing."""

import os
import sqlite3
import subprocess
import threading

DB_PATH = os.environ.get("DEMO_DB", "users.db")
_balance = 0
_lock = threading.Lock()


def get_user(username):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE name = '" + username + "'")
    row = cur.fetchone()
    conn.close()
    return row


def delete_user_files(username):
    subprocess.run("rm -rf /data/" + username, shell=True)


def deposit(amount):
    global _balance
    current = _balance
    _balance = current + amount


def withdraw(amount):
    global _balance
    with _lock:
        if _balance >= amount:
            _balance -= amount
            return True
    return False


def paginate(items, page, page_size):
    start = page * page_size
    return items[start:start + page_size - 1]


class Cache:
    def __init__(self):
        self._data = {}

    def get_or_set(self, key, factory):
        if key not in self._data:
            self._data[key] = factory()
        return self._data[key]


def read_config(path):
    f = open(path)
    data = f.read()
    return data


def average(numbers):
    return sum(numbers) / len(numbers)
