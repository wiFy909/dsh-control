"""Only the explicit user's account key; no plaintext fallback or DSH credential reads."""
from decimal import Decimal, InvalidOperation
import http.client
import json
import threading
from datetime import datetime
import keyring

class Account:
    SERVICE = 'DSH Control / DeepSeek'
    USER = 'balance-api-key'
    def __init__(self):
        self._key = None
        self.lock = threading.Lock()
        self.status = '未配置 API Key'
        self.balances = []
        self.saved = False
        self.stored_key_present = None
        self.generation = 0
        self.last_updated = None

    @staticmethod
    def secure_backend():
        try: backend = keyring.get_keyring()
        except Exception: return False
        # Fail closed on plaintext/chained or third-party backends.
        module = type(backend).__module__
        return module in ('keyring.backends.macOS', 'keyring.backends.Windows', 'keyring.backends.SecretService')

    def restore(self):
        if not self.secure_backend():
            with self.lock:
                self.stored_key_present = None
                if self._key is None:
                    self.status = '系统密钥库不可用，可使用仅本次会话模式'
            return
        generation = self.generation
        try:
            restored = keyring.get_password(self.SERVICE,self.USER)
        except Exception:
            with self.lock:
                if self.generation == generation:
                    self.stored_key_present = None
                    if self._key is None:
                        self.status = '系统密钥库不可用，可使用仅本次会话模式'
            return
        with self.lock:
            if self.generation == generation:
                self._key = restored
                self.saved = bool(restored)
                self.stored_key_present = bool(restored)
                self.balances = []
                self.last_updated = None
                self.status = '已从系统密钥库读取，等待查询' if restored else '未配置 API Key'

    def storage_summary(self):
        with self.lock:
            current = ('当前会话：未使用 Key' if self._key is None else
                       '当前会话：使用已保存 Key' if self.saved else
                       '当前会话：仅本次 Key（不会保存）')
            stored = ('系统密钥库：有已保存 Key，下次启动会恢复' if self.stored_key_present is True else
                      '系统密钥库：无已保存 Key' if self.stored_key_present is False else
                      '系统密钥库：状态未确认；原有 Key 不会自动删除')
        return current + '\n' + stored

    def set_key(self, value, save=False):
        value = value.strip()
        if not value or len(value)>512 or any(c.isspace() for c in value) or not value.isascii():
            raise ValueError('请输入有效 API Key，不含空格或换行。')
        if save:
            if not self.secure_backend(): raise ValueError('当前系统密钥库不可用，请选择仅本次会话。')
            try: keyring.set_password(self.SERVICE,self.USER,value)
            except Exception as exc:
                with self.lock:
                    self.stored_key_present = None
                raise ValueError('密钥库保存失败，持久状态未确认；请核实后选择仅本次会话。') from exc
        with self.lock:
            self.generation += 1
            self._key, self.saved, self.balances = value, save, []
            if save:
                self.stored_key_present = True
            self.last_updated = None
            self.status = '等待余额查询'

    def clear_session(self):
        with self.lock:
            self.generation += 1
            self._key, self.saved, self.balances = None, False, []
            self.last_updated = None
            self.status = '本次凭据已清除'

    def clear(self):
        if not self.secure_backend():
            with self.lock:
                self.stored_key_present = None
            raise ValueError('系统密钥库不可用，无法确认删除；本次凭据未清除。')
        try:
            keyring.delete_password(self.SERVICE,self.USER)
        except keyring.errors.PasswordDeleteError:
            # A missing entry is success only when the vault confirms absence.
            pass
        except Exception as exc:
            with self.lock:
                self.stored_key_present = None
            raise ValueError('系统密钥库删除失败，持久状态未确认；本次凭据未清除。') from exc
        try:
            remaining = keyring.get_password(self.SERVICE,self.USER)
        except Exception as exc:
            with self.lock:
                self.stored_key_present = None
            raise ValueError('无法核实系统密钥库删除结果；本次凭据未清除。') from exc
        if remaining is not None:
            with self.lock:
                self.stored_key_present = True
            raise ValueError('系统密钥库仍有已保存 Key；本次凭据未清除。')
        with self.lock:
            self.generation += 1
            self._key, self.saved, self.balances = None, False, []
            self.stored_key_present = False
            self.last_updated = None
            self.status = '未配置 API Key'

    def refresh(self):
        with self.lock: key = self._key
        if not key: return
        conn = None
        try:
            conn = http.client.HTTPSConnection('api.deepseek.com', timeout=8)
            conn.request('GET','/user/balance',headers={'Authorization':'Bearer '+key,'Accept':'application/json'})
            response = conn.getresponse()
            body = response.read(65537)
            if response.status != 200:
                label = {401:'API Key 无效',403:'余额查询被拒绝',429:'查询过于频繁，请稍后重试'}.get(response.status,'余额查询暂不可用')
                raise ValueError(label)
            if len(body)>65536: raise ValueError('余额响应异常')
            data = json.loads(body)
            balances = []
            for item in data['balance_infos']:
                currency, total = item['currency'], Decimal(item['total_balance'])
                if currency not in ('CNY','USD') or not total.is_finite() or abs(total)>Decimal('1000000000000'): raise ValueError('余额响应格式异常')
                balances.append(f'{currency} {total:.2f}')
            if not balances: raise ValueError('暂无可用余额信息')
            with self.lock:
                if key == self._key:
                    self.last_updated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self.balances = balances
                    self.status = ' / '.join(balances) + ('' if data.get('is_available') else ' · 账户不可用')
        except (OSError, http.client.HTTPException, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            with self.lock:
                if key == self._key:
                    self.balances = []
                    self.status = str(exc) if type(exc) is ValueError and str(exc) in ('API Key 无效','余额查询被拒绝','查询过于频繁，请稍后重试','余额查询暂不可用','余额响应异常','余额响应格式异常','暂无可用余额信息') else '余额查询失败，请检查网络后重试'
        finally:
            if conn: conn.close()
