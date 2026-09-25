"""增量状态管理：manifest.json 记录每 symbol 的 last_date 与条数。"""
import json
import os


class State:
    def __init__(self, state_file):
        self.state_file = state_file
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2, default=str)

    def last_date(self, symbol):
        return self.data.get(symbol, {}).get('last_date')

    def count(self, symbol):
        return self.data.get(symbol, {}).get('count')

    def update(self, symbol, last_date, count):
        self.data[symbol] = {'last_date': last_date, 'count': count}
        self.save()
