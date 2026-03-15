#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║   LCP — Lobster Communication Protocol v3               ║
║   單一整合檔案                                           ║
║   作者：國裕  版本：protocol_001_LCP_v3                  ║
╚══════════════════════════════════════════════════════════╝

用法：
  python lcp.py setup          互動式設定（平台偵測、Token 設定）
  python lcp.py watch          檢查 Moltbook API 版本更新
  python lcp.py test           執行完整測試套件
  python lcp.py run <LCP訊息>  執行單條 LCP 指令
  python lcp.py chat <自然語言> 自然語言轉譯並執行
  python lcp.py register       註冊新龍蝦

區段索引：
  §1  常數與共用工具
  §2  平台偵測 (Platform Adapter)
  §3  驗證挑戰解碼 (Challenge Solver)
  §4  對照庫 (Translation Store)
  §5  沙盒驗證層 (Sandbox)
  §6  Ollama Handler
  §7  Moltbook Watcher
  §8  Moltbook Handler
  §9  轉譯層 (Translator)
  §10 LCP Parser（主入口）
  §11 設定工具 (Setup)
  §12 測試套件 (Test Suite)
  §13 CLI 主程式
"""

# ── 標準函式庫（全部在此集中 import）──────────────────────
import os, sys, re, json, socket, hashlib, sqlite3, subprocess
import urllib.request, urllib.error, urllib.parse
from enum import Enum, auto
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from getpass import getpass
from typing import Optional, Callable


# ══════════════════════════════════════════════════════════
# §1  常數與共用工具
# ══════════════════════════════════════════════════════════

VALID_CMDS          = {"CA", "MB", "SK", "RM", "RP", "EA"}
MAX_DEPTH           = 4
CONFIDENCE_THRESHOLD = 0.7
HOT_CACHE_SIZE      = 100
COLD_DAYS           = 90
CONFIDENCE_REWARD   = 0.05
CONFIDENCE_PENALTY  = 0.10
EA_SCORE_MIN        = -5
EA_SCORE_MAX        = 3
MOLTBOOK_BASE_URL   = "https://www.moltbook.com/api/v1"
SKILL_URL           = "https://www.moltbook.com/skill.md"

# 終端顏色
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def _c(text, color):  return f"{color}{text}{RESET}"
def _ok(msg):         print(f"  {_c('✅', GREEN)} {msg}")
def _err(msg):        print(f"  {_c('❌', RED)} {msg}")
def _warn(msg):       print(f"  {_c('⚠️ ', YELLOW)} {msg}")
def _info(msg):       print(f"  {_c('ℹ️ ', CYAN)} {msg}")
def _head(msg):       print(f"\n{BOLD}{_c(f'── {msg}', CYAN)}{RESET}")
def _hr():            print("─" * 50)

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")

def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()

def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[，。！？、]", "", text)
    return text

def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.error, OSError):
        return False


# ══════════════════════════════════════════════════════════
# §2  平台偵測 (Platform Adapter)
# ══════════════════════════════════════════════════════════

class PlatformType(Enum):
    MACOS   = auto()
    WSL     = auto()
    WINDOWS = auto()

@dataclass
class PlatformInfo:
    platform_type: PlatformType
    ollama_url:    str
    db_dir:        Path
    encoding:      str
    description:   str

def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False

def _get_wsl_host() -> Optional[str]:
    if _port_open("localhost", 11434):
        return "localhost"
    try:
        r = subprocess.run(["wsl.exe","hostname","-I"],
                           capture_output=True, text=True, timeout=5)
        ip = r.stdout.strip().split()[0]
        if ip and _port_open(ip, 11434):
            return ip
    except Exception:
        pass
    return None

def detect_platform() -> PlatformInfo:
    if sys.platform == "darwin":
        return PlatformInfo(PlatformType.MACOS,
            "http://localhost:11434", Path.home()/".lcp", "utf-8", "macOS")

    if sys.platform == "linux" and _is_wsl():
        return PlatformInfo(PlatformType.WSL,
            "http://localhost:11434", Path.home()/".lcp", "utf-8", "WSL (Ubuntu)")

    if sys.platform == "win32":
        host = _get_wsl_host()
        url  = f"http://{host}:11434" if host else "http://localhost:11434"
        if not host:
            _warn("無法偵測到 WSL 內的 Ollama，請確認 OLLAMA_HOST=0.0.0.0")
        appdata = Path(os.environ.get("APPDATA", Path.home())) / "lcp"
        return PlatformInfo(PlatformType.WINDOWS, url, appdata, "utf-8",
                            f"Windows (Ollama→{url})")

    return PlatformInfo(PlatformType.WSL,
        "http://localhost:11434", Path.home()/".lcp", "utf-8",
        f"Unknown ({sys.platform})")

_platform_cache: Optional[PlatformInfo] = None

def get_platform() -> PlatformInfo:
    global _platform_cache
    if _platform_cache is None:
        _platform_cache = detect_platform()
        _platform_cache.db_dir.mkdir(parents=True, exist_ok=True)
        if _platform_cache.platform_type == PlatformType.WINDOWS:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
                sys.stderr.reconfigure(encoding="utf-8")
    return _platform_cache


# ══════════════════════════════════════════════════════════
# §3  驗證挑戰解碼 (Challenge Solver)
# ══════════════════════════════════════════════════════════

_NUMBER_WORDS = {
    "zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,
    "seven":7,"eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12,
    "thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,
    "eighteen":18,"nineteen":19,"twenty":20,"thirty":30,"forty":40,
    "fifty":50,"sixty":60,"seventy":70,"eighty":80,"ninety":90,
    "hundred":100,"thousand":1000,
}

_OPERATOR_WORDS = {
    "plus":"+","add":"+","adds":"+","added":"+","and":"+",
    "gains":"+","gain":"+","increases":"+","increase":"+",
    "more":"+","extra":"+",
    "minus":"-","subtract":"-","subtracts":"-","slows":"-","slow":"-",
    "loses":"-","lose":"-","decreases":"-","decrease":"-","less":"-",
    "fewer":"-","reduced":"-","reduce":"-","drops":"-","drop":"-","falls":"-",
    "times":"*","multiplied":"*","multiply":"*","multiplies":"*","doubled":"*",
    "divided":"/","divides":"/","splits":"/","halved":"/","shared":"/",
}

def _parse_arabic(token: str) -> Optional[float]:
    try:
        return float(token.replace(",",""))
    except ValueError:
        return None

def _parse_number_word(tokens: list, i: int) -> tuple:
    tok = tokens[i]
    if tok in _NUMBER_WORDS:
        val = _NUMBER_WORDS[tok]
        if i+1 < len(tokens):
            nxt = tokens[i+1]
            if nxt in _NUMBER_WORDS and _NUMBER_WORDS[nxt] < 10 and 20 <= val <= 90:
                return float(val + _NUMBER_WORDS[nxt]), 2
        return float(val), 1
    combined = tok
    for j in range(i+1, min(i+4, len(tokens))):
        combined += tokens[j]
        if combined in _NUMBER_WORDS:
            return float(_NUMBER_WORDS[combined]), j-i+1
    return None, 1

def decode_challenge(challenge_text: str) -> tuple:
    cleaned = re.sub(r'[\]\[\^/\-]', '', challenge_text)
    cleaned = re.sub(r'\s+', ' ', cleaned).lower().strip()
    cleaned = re.sub(r'(.)\1+', r'\1', cleaned)
    tokens  = cleaned.split()

    numbers  = []
    operator = None

    # 動詞類強運算符優先
    strong = {k:v for k,v in _OPERATOR_WORDS.items() if k != "and"}
    for tok in tokens:
        if tok in strong:
            operator = strong[tok]
            break

    i = 0
    while i < len(tokens):
        tok = tokens[i].strip()
        if not tok:
            i += 1; continue

        num = _parse_arabic(tok)
        if num is not None:
            numbers.append(num); i += 1; continue

        num, consumed = _parse_number_word(tokens, i)
        if num is not None:
            numbers.append(num); i += consumed; continue

        if tok in _OPERATOR_WORDS:
            if operator is None:
                operator = _OPERATOR_WORDS[tok]
            i += 1; continue
        i += 1

    if len(numbers) >= 2 and operator:
        a, b = numbers[0], numbers[1]
        ops  = {"+": a+b, "-": a-b, "*": a*b, "/": a/b if b else 0}
        res  = ops.get(operator, 0)
        return res, f"{a} {operator} {b} = {res}"

    if len(numbers) >= 2:
        res = numbers[0] + numbers[1]
        return res, f"{numbers[0]} + {numbers[1]} = {res} (預設加法)"

    return None, f"解碼失敗: numbers={numbers} op={operator}"

def format_answer(value: float) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ══════════════════════════════════════════════════════════
# §4  對照庫 (Translation Store)
# ══════════════════════════════════════════════════════════

@dataclass
class TranslationRecord:
    input_hash: str;  normalized: str;  lcp_output: str;  cmd: str
    confidence: float; hit_count: int;  source: str
    created_at: str;  last_used: str;   status: str

@dataclass
class TranslationResult:
    lcp_output: str;  confidence: float;  source: str
    record_id: Optional[int] = None

class TranslationStore:
    def __init__(self, db_path: str = "lcp_translation.db"):
        self.db_path  = db_path
        self.hot_cache: dict[str, TranslationRecord] = {}
        self._init_db()

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS lcp_translation (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    input_hash TEXT UNIQUE NOT NULL, normalized TEXT NOT NULL,
                    lcp_output TEXT NOT NULL, cmd TEXT NOT NULL,
                    confidence REAL DEFAULT 0.5, hit_count INTEGER DEFAULT 1,
                    source TEXT DEFAULT 'auto', created_at TEXT NOT NULL,
                    last_used TEXT NOT NULL, status TEXT DEFAULT 'active');
                CREATE TABLE IF NOT EXISTS lcp_translation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    input_hash TEXT, input_raw TEXT, lcp_output TEXT,
                    result TEXT, confidence_at REAL, logged_at TEXT);
                CREATE INDEX IF NOT EXISTS idx_hash ON lcp_translation(input_hash);
                CREATE INDEX IF NOT EXISTS idx_status ON lcp_translation(status);
            """)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def lookup(self, raw_input: str) -> Optional[TranslationResult]:
        norm = _normalize(raw_input); h = _md5(norm)
        if h in self.hot_cache:
            self._update_hit(h)
            r = self.hot_cache[h]
            return TranslationResult(r.lcp_output, r.confidence, "hot_cache")
        rec = self._db_get(h)
        if rec and rec["status"] == "active" and rec["confidence"] >= CONFIDENCE_THRESHOLD:
            self._update_hit(h); self._promote_to_hot(rec)
            return TranslationResult(rec["lcp_output"], rec["confidence"], "db", rec["id"])
        return None

    def insert(self, raw_input: str, lcp_output: str,
               confidence: float, source: str = "auto") -> bool:
        if confidence < CONFIDENCE_THRESHOLD:
            self._log(raw_input, lcp_output, "rejected", confidence); return False
        cmd = self._extract_cmd(lcp_output)
        if not cmd:
            self._log(raw_input, lcp_output, "invalid_format", confidence); return False
        norm = _normalize(raw_input); h = _md5(norm); ts = _now()
        try:
            with self._conn() as c:
                c.execute("""INSERT OR IGNORE INTO lcp_translation
                    (input_hash,normalized,lcp_output,cmd,confidence,hit_count,
                     source,created_at,last_used,status) VALUES(?,?,?,?,?,1,?,?,?,'active')""",
                    (h,norm,lcp_output,cmd,confidence,source,ts,ts))
            self._log(raw_input, lcp_output, "inserted", confidence); return True
        except Exception as e:
            print(f"[Store] insert error: {e}"); return False

    def apply_ea_feedback(self, lcp_output: str, ea_type: str):
        with self._conn() as c:
            row = c.execute(
                "SELECT id,confidence FROM lcp_translation WHERE lcp_output=? AND status='active'",
                (lcp_output,)).fetchone()
            if not row: return
            rid, conf = row["id"], row["confidence"]
            if ea_type == "reward":
                c.execute("UPDATE lcp_translation SET confidence=? WHERE id=?",
                          (min(1.0, conf+CONFIDENCE_REWARD), rid))
            elif ea_type == "penalty":
                new = max(0.0, conf-CONFIDENCE_PENALTY)
                st  = "quarantine" if new < 0.3 else "active"
                c.execute("UPDATE lcp_translation SET confidence=?,status=?,hit_count=0 WHERE id=?",
                          (new, st, rid))
        self.hot_cache = {k:v for k,v in self.hot_cache.items() if v.lcp_output != lcp_output}

    def run_maintenance(self):
        cold = (datetime.now()-timedelta(days=COLD_DAYS)).isoformat()
        quar = (datetime.now()-timedelta(days=30)).isoformat()
        with self._conn() as c:
            c.execute("UPDATE lcp_translation SET status='cold' WHERE status='active' AND last_used<?", (cold,))
            c.execute("DELETE FROM lcp_translation WHERE status='quarantine' AND last_used<?", (quar,))
        self.hot_cache.clear()

    def stats(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT status,COUNT(*) cnt,AVG(confidence) ac FROM lcp_translation GROUP BY status").fetchall()
        return {r["status"]: {"count":r["cnt"],"avg_confidence":round(r["ac"],3)} for r in rows}

    def _db_get(self, h):
        with self._conn() as c:
            return c.execute("SELECT * FROM lcp_translation WHERE input_hash=?", (h,)).fetchone()

    def _update_hit(self, h):
        with self._conn() as c:
            c.execute("UPDATE lcp_translation SET hit_count=hit_count+1,last_used=? WHERE input_hash=?",
                      (_now(), h))

    def _promote_to_hot(self, rec):
        if len(self.hot_cache) >= HOT_CACHE_SIZE:
            oldest = min(self.hot_cache.values(), key=lambda r: r.hit_count)
            del self.hot_cache[oldest.input_hash]
        self.hot_cache[rec["input_hash"]] = TranslationRecord(
            rec["input_hash"],rec["normalized"],rec["lcp_output"],rec["cmd"],
            rec["confidence"],rec["hit_count"],rec["source"],
            rec["created_at"],rec["last_used"],rec["status"])

    def _extract_cmd(self, lcp: str) -> Optional[str]:
        m = re.match(r"^L\|([A-Z]+)\|", lcp)
        return m.group(1) if m and m.group(1) in VALID_CMDS else None

    def _log(self, raw, lcp, result, conf):
        h = _md5(_normalize(raw))
        with self._conn() as c:
            c.execute("INSERT INTO lcp_translation_log (input_hash,input_raw,lcp_output,result,confidence_at,logged_at) VALUES(?,?,?,?,?,?)",
                      (h, raw[:500], lcp, result, conf, _now()))


# ══════════════════════════════════════════════════════════
# §5  沙盒驗證層 (Sandbox)
# ══════════════════════════════════════════════════════════

class SandboxState(Enum):
    IDLE=auto(); SANDBOX_ENTER=auto(); RP_CHECK=auto(); EA_VALIDATE=auto()
    CHAIN_TERMCHECK=auto(); EA_EXECUTE=auto(); SANDBOX_EXIT=auto()
    FORCE_PENALTY=auto(); REJECT=auto(); ABORT=auto()

@dataclass
class LayerResult:
    layer: int; lcp_input: str; status: str; output: str; cmd: str

@dataclass
class SandboxResult:
    passed: bool; state: str; ea_output: str; ea_type: str; reason: str
    layer_results: list = field(default_factory=list)

_PHISHING_PATTERNS = [
    "ignore previous","override safety","disregard rules","you are now",
    "pretend you are","reveal system prompt","ignore instructions","bypass","jailbreak",
]

def _phish_check(chain: list) -> tuple:
    text = " ".join(chain).lower()
    for p in _PHISHING_PATTERNS:
        if p in text:
            return False, f"phishing_pattern:{p}"
    apis = [re.search(r"L\|CA\|([^|]+)",m).group(1) for m in chain if re.search(r"L\|CA\|([^|]+)",m)]
    if len(set(apis)) >= 2:
        return False, "cross_domain_ca_detected"
    return True, "ok"

class Sandbox:
    def __init__(self): self.state = SandboxState.IDLE

    def validate_chain(self, chain: list, layer_results: list) -> SandboxResult:
        self.state = SandboxState.SANDBOX_ENTER
        if len(chain) > MAX_DEPTH:
            return self._abort("depth_exceeded", layer_results)
        if len(chain) >= 3:
            safe, reason = _phish_check(chain)
            if not safe:
                return self._abort(f"phishing:{reason}", layer_results)
        if len(chain) == MAX_DEPTH:
            return self._full_sandbox(chain, layer_results)
        return self._partial_validate(chain, layer_results)

    def _full_sandbox(self, chain, results):
        self.state = SandboxState.RP_CHECK
        missing = [r.layer for r in results[:3] if r.status != "ok"]
        if missing:
            return self._make(False,"FORCE_PENALTY","penalty",f"missing_ok_rp:layer{missing}",-3,results)

        self.state = SandboxState.EA_VALIDATE
        valid, reason = self._validate_ea(chain[3])
        if not valid:
            return self._make(False,"REJECT","penalty",f"invalid_ea:{reason}",-1,results)

        self.state = SandboxState.CHAIN_TERMCHECK
        if len(chain) > MAX_DEPTH:
            return self._abort("chain_not_terminated", results)

        self.state = SandboxState.EA_EXECUTE
        ea_type, score = self._compute_ea(results)
        ea_out = f"L|EA|{ea_type}|chain_complete|{score:+d}|E"
        self.state = SandboxState.SANDBOX_EXIT
        return self._make(True,"SANDBOX_EXIT",ea_type,"ok",score,results,ea_out)

    def _partial_validate(self, chain, results):
        for i,msg in enumerate(chain):
            if not re.match(r"^L\|[A-Z]+\|.+\|E$", msg.strip()):
                return self._make(False,"REJECT","penalty",f"invalid_format:layer{i+1}",-1,results)
        ea_type, score = self._compute_ea(results)
        return self._make(True,"SANDBOX_EXIT",ea_type,"ok",score,results)

    def _compute_ea(self, results):
        if not results: return "penalty", -1
        sts = [r.status for r in results]
        if all(s=="ok" for s in sts): return "reward", +1
        return "penalty", (-1 if any(s=="ok" for s in sts) else -3)

    def _validate_ea(self, lcp):
        m = re.match(r"^L\|EA\|(\w+)\|([^|]+)\|([+-]?\d+)\|E$", lcp)
        if not m: return False, "regex_mismatch"
        if m.group(1) not in ("reward","penalty"): return False, f"invalid_type:{m.group(1)}"
        if not (EA_SCORE_MIN <= int(m.group(3)) <= EA_SCORE_MAX): return False, "score_out_of_range"
        return True, "ok"

    def _make(self, passed, state, ea_type, reason, score, results, ea_out=None):
        if ea_out is None: ea_out = f"L|EA|{ea_type}|{reason}|{score:+d}|E"
        return SandboxResult(passed,state,ea_out,ea_type,reason,results)

    def _abort(self, reason, results):
        return self._make(False,"ABORT","penalty",reason,-5,results)


# ══════════════════════════════════════════════════════════
# §6  Ollama Handler
# ══════════════════════════════════════════════════════════

@dataclass
class OllamaResponse:
    success: bool; content: str; model: str; error: Optional[str] = None

class OllamaHandler:
    def __init__(self, model: str = "qwen2.5:7b", timeout: int = 60):
        self.model   = model
        self.timeout = timeout
        self._base   = get_platform().ollama_url

    def chat(self, prompt: str, system: str = "") -> OllamaResponse:
        msgs = []
        if system: msgs.append({"role":"system","content":system})
        msgs.append({"role":"user","content":prompt})
        return self._post("/api/chat", {"model":self.model,"messages":msgs,
            "stream":False,"options":{"temperature":0.1,"num_predict":128}})

    def generate(self, prompt: str) -> OllamaResponse:
        return self._post("/api/generate", {"model":self.model,"prompt":prompt,
            "stream":False,"options":{"temperature":0.7,"num_predict":256}})

    def is_available(self) -> bool:
        try:
            urllib.request.urlopen(f"{self._base}/api/tags", timeout=3)
            return True
        except: return False

    def list_models(self) -> list:
        try:
            with urllib.request.urlopen(f"{self._base}/api/tags", timeout=5) as r:
                return [m["name"] for m in json.loads(r.read()).get("models",[])]
        except: return []

    def lcp_translate(self, text: str) -> OllamaResponse:
        system = ("你是 LCP 轉譯器。只能輸出極簡 LCP 格式，不能輸出任何其他文字。\n"
                  "格式：L|CMD|param1|param2|E\nCMD 只能是：CA MB SK RM RP EA\n"
                  "例：查天氣→L|CA|openweather|taipei|E 發文→L|MB|標題|內容|E")
        return self.chat(text, system=system)

    def lcp_social_reply(self, post_content: str, context: str = "") -> OllamaResponse:
        ctx = f"相關背景：{context}\n" if context else ""
        return self.generate(f"以下是一篇 Moltbook 貼文：\n\n{post_content}\n\n{ctx}"
                             "請用自然、友善的語氣回應（100字以內）：")

    def _post(self, endpoint: str, payload: dict) -> OllamaResponse:
        req = urllib.request.Request(
            f"{self._base}{endpoint}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type":"application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
            content = (data.get("message",{}).get("content") or data.get("response","")).strip()
            return OllamaResponse(True, content, data.get("model", self.model))
        except urllib.error.URLError as e:
            return OllamaResponse(False,"",self.model,f"connection_error:{e.reason}")
        except Exception as e:
            return OllamaResponse(False,"",self.model,f"error:{e}")


# ══════════════════════════════════════════════════════════
# §7  Moltbook Watcher
# ══════════════════════════════════════════════════════════

@dataclass
class WatchResult:
    updated: bool; version: str; prev_version: str
    changes: list; config_path: str

class MoltbookWatcher:
    def __init__(self):
        self.config_dir  = get_platform().db_dir
        self.config_path = self.config_dir / "moltbook_config.json"
        self.ver_path    = self.config_dir / "moltbook_version.json"
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def check_and_update(self, force: bool = False) -> WatchResult:
        prev = self._load_ver()
        if not force and prev:
            last = datetime.fromisoformat(prev.get("fetched_at","2000-01-01"))
            if datetime.now() - last < timedelta(hours=6):
                ver = prev.get("version","unknown")
                return WatchResult(False, ver, ver, [], str(self.config_path))

        content = self._fetch()
        if not content:
            ver = (prev or {}).get("version","unknown")
            return WatchResult(False, ver, ver, ["fetch_failed"], str(self.config_path))

        new_ver  = self._parse_ver(content)
        prev_ver = (prev or {}).get("version","0.0.0")
        config   = self._parse_config(content, new_ver)
        changes  = self._diff(prev, config)
        self._save_ver(new_ver, config)
        self._save_config(config)

        if new_ver != prev_ver:
            print(f"[Watcher] ⬆️  {prev_ver} → {new_ver}")
            for c in changes: print(f"[Watcher]  • {c}")
        else:
            print(f"[Watcher] ✅ 版本確認：{new_ver}")
        return WatchResult(new_ver!=prev_ver, new_ver, prev_ver, changes, str(self.config_path))

    def load_config(self) -> dict:
        """
        讀取本地設定。
        設定不存在時直接回傳預設值，不觸發網路 fetch。
        需要更新時請明確呼叫 check_and_update()。
        """
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return self._default_config()

    def _fetch(self) -> Optional[str]:
        try:
            import ssl
            ctx = ssl.create_default_context()
            req = urllib.request.Request(SKILL_URL, headers={"User-Agent":"LCP-Watcher/3.0"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                return r.read().decode("utf-8")
        except Exception as e:
            print(f"[Watcher] fetch 失敗：{e}"); return None

    def _parse_ver(self, content: str) -> str:
        m = re.search(r"version:\s*([\d.]+)", content)
        return m.group(1) if m else "unknown"

    def _parse_config(self, content: str, version: str) -> dict:
        cfg = self._default_config()
        cfg["version"] = version
        m = re.search(r'\*\*Base URL:\*\*\s*`([^`]+)`', content)
        if m: cfg["base_url"] = m.group(1).strip()
        rl = cfg["rate_limits"]
        for pat, key in [
            (r'1 post per (\d+) minutes?',     "post_cooldown_minutes"),
            (r'1 comment per (\d+) seconds?',  "comment_cooldown_seconds"),
            (r'(\d+) comments? per day',        "comments_per_day"),
        ]:
            m = re.search(pat, content)
            if m: rl[key] = int(m.group(1))
        m = re.search(r'(\d+) minutes? to solve', content)
        if m: cfg["verification"]["challenge_expire_minutes"] = int(m.group(1))
        cfg["fetched_at"] = _now()
        return cfg

    def _diff(self, prev, new) -> list:
        if not prev: return ["初次設定"]
        changes = []
        if prev.get("base_url") != new.get("base_url"):
            changes.append(f"base_url 變更：{prev.get('base_url')} → {new.get('base_url')}")
        for k in set(list(prev.get("rate_limits",{}))+list(new.get("rate_limits",{}))):
            ov, nv = prev.get("rate_limits",{}).get(k), new.get("rate_limits",{}).get(k)
            if ov != nv: changes.append(f"rate_limit.{k}：{ov} → {nv}")
        return changes

    def _save_ver(self, ver, cfg):
        self.ver_path.write_text(json.dumps({"version":ver,"fetched_at":cfg.get("fetched_at","")}), encoding="utf-8")

    def _save_config(self, cfg):
        self.config_path.write_text(json.dumps(cfg,indent=2,ensure_ascii=False), encoding="utf-8")

    def _load_ver(self):
        return json.loads(self.ver_path.read_text(encoding="utf-8")) if self.ver_path.exists() else None

    @staticmethod
    def _default_config() -> dict:
        return {"version":"unknown","fetched_at":"",
            "base_url": MOLTBOOK_BASE_URL,
            "rate_limits": {"post_cooldown_minutes":30,"comment_cooldown_seconds":20,
                            "comments_per_day":50,"read_rps":60,"write_rps":30},
            "verification": {"challenge_expire_minutes":5,"submolt_expire_seconds":30},
            "new_agent_restrict_hours": 24}


# ══════════════════════════════════════════════════════════
# §8  Moltbook Handler
# ══════════════════════════════════════════════════════════

@dataclass
class MoltbookPost:
    title: str; content: str
    submolt_name: str = "general"
    url: Optional[str] = None
    post_type: str = "text"

@dataclass
class MoltbookComment:
    content: str; parent_id: Optional[str] = None

@dataclass
class MoltbookResult:
    success: bool; post_id: Optional[str]=None; comment_id: Optional[str]=None
    url: Optional[str]=None; error: Optional[str]=None
    raw: Optional[dict]=None; verified: bool=False

def _load_api_key() -> str:
    for env in ("MOLTBOOK_API_KEY","MOLTBOOK_API_TOKEN"):
        k = os.environ.get(env,"")
        if k: return k
    paths = [get_platform().db_dir/".env", Path.cwd()/".env",
             Path.home()/".config"/"moltbook"/"credentials.json"]
    for p in paths:
        if not p.exists(): continue
        if p.suffix == ".json":
            k = json.loads(p.read_text(encoding="utf-8")).get("api_key","")
            if k: return k
        else:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith("MOLTBOOK_API_KEY=") or line.startswith("MOLTBOOK_API_TOKEN="):
                    return line.split("=",1)[1].strip().strip('"').strip("'")
    raise ValueError("找不到 Moltbook API Key。請執行：python lcp.py setup")

class MoltbookHandler:
    def __init__(self):
        self._base    = MoltbookWatcher().load_config().get("base_url", MOLTBOOK_BASE_URL)
        self._key     = _load_api_key()
        self._timeout = 15

    @staticmethod
    def register(name: str, description: str) -> dict:
        payload = json.dumps({"name":name,"description":description}).encode()
        req = urllib.request.Request(f"{MOLTBOOK_BASE_URL}/agents/register",
                                     data=payload, method="POST",
                                     headers={"Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:
            return {"success":False,"error":str(e)}

    def home(self) -> dict:     return self._get("/home") or {}
    def status(self) -> dict:   return self._get("/agents/status") or {}
    def me(self) -> dict:       return self._get("/agents/me") or {}
    def is_available(self) -> bool:
        return self._get("/agents/status") is not None

    def post(self, post: MoltbookPost) -> MoltbookResult:
        payload = {"submolt_name":post.submolt_name,"title":post.title,
                   "content":post.content,"type":post.post_type}
        if post.url: payload["url"] = post.url
        resp = self._post("/posts", payload)
        if not resp: return MoltbookResult(False, error="request_failed")
        pid    = (resp.get("post") or {}).get("id") or resp.get("id")
        result = MoltbookResult(True, post_id=pid, raw=resp)
        if resp.get("verification_required") and pid:
            result = self._verify(resp, result)
        return result

    def comment(self, post_id: str, comment: MoltbookComment) -> MoltbookResult:
        payload = {"content": comment.content}
        if comment.parent_id: payload["parent_id"] = comment.parent_id
        resp = self._post(f"/posts/{post_id}/comments", payload)
        if not resp: return MoltbookResult(False, error="request_failed")
        cid    = (resp.get("comment") or {}).get("id") or resp.get("id")
        result = MoltbookResult(True, comment_id=cid, raw=resp)
        if resp.get("verification_required") and cid:
            result = self._verify(resp, result)
        return result

    def get_feed(self, sort="hot", limit=25, cursor="", filter="all") -> dict:
        p = f"?sort={sort}&limit={limit}&filter={filter}"
        if cursor: p += f"&cursor={cursor}"
        return self._get(f"/feed{p}") or {"posts":[]}

    def get_post(self, post_id: str) -> Optional[dict]:
        return self._get(f"/posts/{post_id}")

    def get_comments(self, post_id: str, sort="best", limit=35) -> dict:
        return self._get(f"/posts/{post_id}/comments?sort={sort}&limit={limit}") or {}

    def search(self, query: str, search_type="all", limit=20) -> dict:
        q = urllib.parse.quote(query)
        return self._get(f"/search?q={q}&type={search_type}&limit={limit}") or {}

    def upvote_post(self, pid: str) -> dict:    return self._post(f"/posts/{pid}/upvote", {}) or {}
    def upvote_comment(self, cid: str) -> dict: return self._post(f"/comments/{cid}/upvote", {}) or {}
    def follow(self, name: str) -> dict:        return self._post(f"/agents/{name}/follow", {}) or {}
    def mark_all_read(self) -> dict:            return self._post("/notifications/read-all", {}) or {}
    def mark_read(self, pid: str) -> dict:      return self._post(f"/notifications/read-by-post/{pid}", {}) or {}

    def _verify(self, resp: dict, result: MoltbookResult) -> MoltbookResult:
        ver = (resp.get("post") or resp.get("comment") or resp.get("verification") or {})
        if isinstance(ver, dict) and "verification" in ver:
            ver = ver["verification"]
        code      = ver.get("verification_code","")
        challenge = ver.get("challenge_text","")
        if not code or not challenge:
            result.error = "missing_verification_fields"; return result
        val, expl = decode_challenge(challenge)
        if val is None:
            result.error = f"decode_failed:{expl}"; return result
        ans  = format_answer(val)
        print(f"[Moltbook] 驗證：{expl} → {ans}")
        vr = self._post("/verify", {"verification_code":code,"answer":ans})
        if vr and vr.get("success"):
            result.verified = True; print("[Moltbook] ✅ 驗證成功")
        else:
            result.error = f"verify_failed:{(vr or {}).get('error','no_response')}"
            print(f"[Moltbook] ❌ {result.error}")
        return result

    def _get(self, path): return self._req("GET", path)
    def _post(self, path, payload): return self._req("POST", path, payload)
    def _delete(self, path): return self._req("DELETE", path)

    def _req(self, method, path, payload=None):
        url  = f"{self._base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req  = urllib.request.Request(url, data=data, method=method,
            headers={"Authorization":f"Bearer {self._key}",
                     "Content-Type":"application/json","Accept":"application/json",
                     "User-Agent":"LCP-Lobster/3.0"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try: return json.loads(body)
            except: print(f"[Moltbook] HTTP {e.code}: {body[:100]}"); return None
        except Exception as e:
            print(f"[Moltbook] {e}"); return None

def parse_mb_params(params: list) -> MoltbookPost:
    if not params: return MoltbookPost("無標題","")
    if len(params) >= 3: return MoltbookPost(params[1],params[2],submolt_name=params[0])
    if len(params) == 2: return MoltbookPost(params[0],params[1])
    return MoltbookPost(params[0],"")

def parse_comment_params(params: list) -> tuple:
    pid     = params[0].replace("post_id:","") if params else ""
    content = params[1] if len(params) > 1 else ""
    parent  = params[2].replace("parent:","") if len(params) > 2 else None
    return pid, MoltbookComment(content, parent)


# ══════════════════════════════════════════════════════════
# §9  轉譯層 (Translator)
# ══════════════════════════════════════════════════════════

_SEED_TRANSLATIONS = [
    ("查天氣",     "L|CA|openweather|taipei|E",  0.9),
    ("查台北天氣", "L|CA|openweather|taipei|E",  0.95),
    ("發文",       "L|MB|general|新貼文|內容待填|E", 0.75),
    ("記住",       "L|SK|memo|內容待填|E",        0.75),
    ("存起來",     "L|SK|memo|內容待填|E",        0.75),
    ("讀取記憶",   "L|RM|memo|E",                0.85),
    ("查上次結果", "L|RM|last_result|E",          0.85),
]

class Translator:
    def __init__(self, store: TranslationStore, ollama: OllamaHandler):
        self.store  = store
        self.ollama = ollama
        if not store.stats():
            for text, lcp, conf in _SEED_TRANSLATIONS:
                store.insert(text, lcp, conf, source="system")

    def translate(self, raw: str) -> TranslationResult:
        result = self.store.lookup(raw)
        if result: return result
        lcp, conf = self._rules(raw)
        source = "rule"
        if conf < 0.7 and self.ollama.is_available():
            resp = self.ollama.lcp_translate(raw)
            if resp.success and _parse_lcp(resp.content):
                lcp, conf, source = resp.content.strip(), 0.75, "ollama"
        if conf >= 0.7:
            self.store.insert(raw, lcp, conf, source=source)
        return TranslationResult(lcp, conf, source)

    def _rules(self, text: str) -> tuple:
        t = text.lower().strip()
        if "天氣" in t:
            city = next((c for c in ["台北","台中","高雄","新竹","台南"] if c in t), "taipei")
            return f"L|CA|openweather|{city}|E", 0.75
        if any(k in t for k in ["發文","post","發佈","貼文"]):
            return "L|MB|general|新貼文|內容待填|E", 0.72
        if any(k in t for k in ["記住","存","save","記錄"]):
            return "L|SK|memo|內容待填|E", 0.72
        if any(k in t for k in ["讀取","recall","查記憶","上次"]):
            return "L|RM|last_result|E", 0.72
        return "L|RP|status:uncertain|E", 0.3


# ══════════════════════════════════════════════════════════
# §10  LCP Parser（主入口）
# ══════════════════════════════════════════════════════════

@dataclass
class LCPMessage:
    cmd: str; params: list; raw: str

@dataclass
class ExecutionResult:
    success: bool; output: str; ea_output: str
    sandbox: Optional[SandboxResult] = None
    error: Optional[str] = None

def _parse_lcp(raw: str) -> Optional[LCPMessage]:
    raw = raw.strip()
    if raw.startswith("L|") and not raw.endswith("|E"):
        raw = raw.rstrip("|") + "|E"
    if not (raw.startswith("L|") and raw.endswith("|E")):
        return None
    parts = raw[2:-2].split("|")
    if not parts: return None
    cmd = parts[0].upper()
    if cmd not in VALID_CMDS: return None
    return LCPMessage(cmd, parts[1:], raw)

class LCPParser:
    def __init__(self):
        pf   = get_platform()
        db   = str(pf.db_dir / "translation.db")
        self.store      = TranslationStore(db)
        self.ollama     = OllamaHandler()
        self.translator = Translator(self.store, self.ollama)
        self.sandbox    = Sandbox()
        self.moltbook   = self._init_mb()
        self.watcher    = MoltbookWatcher()
        print(f"[LCP] 平台：{pf.description}")
        print(f"[LCP] Ollama：{'✅' if self.ollama.is_available() else '❌ 離線'}")
        print(f"[LCP] Moltbook：{'✅' if self.moltbook else '⚠️  未設定 Token'}")

    def _init_mb(self):
        try:    return MoltbookHandler()
        except: return None

    def run(self, raw: str) -> ExecutionResult:
        msg = _parse_lcp(raw)
        if not msg:
            return ExecutionResult(False,"L|RP|status:err|code:PARSE_ERROR|E",
                                   "L|EA|penalty|parse_error|-1|E",error="parse_error")
        return ExecutionResult(True, self._dispatch(msg), "")

    def run_chain(self, chain: list) -> ExecutionResult:
        if len(chain) > MAX_DEPTH:
            return ExecutionResult(False,"L|RP|status:err|code:DEPTH_EXCEEDED|E",
                                   "L|EA|penalty|depth_exceeded|-5|E",error="depth_exceeded")
        results = []; last = ""
        for i, raw in enumerate(chain):
            msg = _parse_lcp(raw)
            if not msg:
                results.append(LayerResult(i+1,raw,"err","PARSE_ERROR","?")); break
            out    = self._dispatch(msg)
            status = "ok" if "status:err" not in out else "err"
            results.append(LayerResult(i+1,raw,status,out,msg.cmd))
            last = out
        sb = self.sandbox.validate_chain(chain, results)
        for r in results: self.store.apply_ea_feedback(r.lcp_input, sb.ea_type)
        return ExecutionResult(sb.passed, last, sb.ea_output, sb)

    def run_natural(self, text: str) -> ExecutionResult:
        tr = self.translator.translate(text)
        if tr.confidence < CONFIDENCE_THRESHOLD:
            return ExecutionResult(False,
                f"L|RP|status:uncertain|confidence:{tr.confidence:.2f}|E",
                "L|EA|penalty|low_confidence|-1|E", error="low_confidence")
        return self.run(tr.lcp_output)

    def run_social_reply(self, post_id: str) -> ExecutionResult:
        if not self.moltbook:
            return ExecutionResult(False,"L|RP|status:err|code:NO_TOKEN|E","",error="no_token")
        post = self.moltbook.get_post(post_id)
        if not post:
            return ExecutionResult(False,"L|RP|status:err|code:NOT_FOUND|E","",error="not_found")
        resp = self.ollama.lcp_social_reply(post.get("content",""))
        if not resp.success:
            return ExecutionResult(False,"L|RP|status:err|code:OLLAMA_FAIL|E","",error="ollama_fail")
        body = resp.content[:200]
        return self.run_chain([
            f"L|RM|post:{post_id}|E",
            f"L|SK|reply_draft:{post_id}|{body}|E",
            f"L|MB|general|RE:{post.get('title','')}|{body}|E",
        ])

    def _dispatch(self, msg: LCPMessage) -> str:
        return {"CA":self._ca,"MB":self._mb,"SK":self._sk,
                "RM":self._rm,"RP":self._rp,"EA":self._ea}[msg.cmd](msg)

    def _ca(self, msg):
        api = msg.params[0] if msg.params else "unknown"
        if api == "ollama":
            prompt = msg.params[1] if len(msg.params)>1 else ""
            r = self.ollama.chat(prompt)
            return f"L|RP|status:ok|data:{r.content[:80]}|E" if r.success else \
                   f"L|RP|status:err|code:{r.error}|E"
        if api in ("openweather","weather"):
            city = msg.params[1] if len(msg.params)>1 else "taipei"
            return f"L|RP|status:ok|source:openweather|city:{city}|data:stub_晴天28度|E"
        if api == "moltbook_home":
            if not self.moltbook: return "L|RP|status:err|code:NO_TOKEN|E"
            h = self.moltbook.home()
            notif = h.get("your_account",{}).get("unread_notification_count",0)
            return f"L|RP|status:ok|unread:{notif}|E"
        if api == "moltbook_feed":
            if not self.moltbook: return "L|RP|status:err|code:NO_TOKEN|E"
            feed = self.moltbook.get_feed()
            count = len(feed.get("posts",[]))
            return f"L|RP|status:ok|posts:{count}|E"
        return f"L|RP|status:err|code:UNKNOWN_API:{api}|E"

    def _mb(self, msg):
        if not self.moltbook: return "L|RP|status:err|code:NO_TOKEN|E"
        post = parse_mb_params(msg.params)
        r    = self.moltbook.post(post)
        return f"L|RP|status:ok|post_id:{r.post_id}|verified:{r.verified}|E" if r.success \
               else f"L|RP|status:err|code:{r.error}|E"

    def _sk(self, msg):
        key   = msg.params[0] if msg.params else "unknown"
        value = msg.params[1] if len(msg.params)>1 else ""
        if re.search(r"L\|[A-Z]{2}\|", value):
            return "L|RP|status:err|code:MEMORY_POISON|E"
        if len(value) > 512:
            return "L|RP|status:err|code:VALUE_TOO_LONG|E"
        if not re.match(r"^[a-z0-9_:\-]+$", key):
            return "L|RP|status:err|code:INVALID_KEY|E"
        return f"L|RP|status:ok|key:{key}|saved:true|E"

    def _rm(self, msg):
        key = msg.params[0] if msg.params else "unknown"
        return f"L|RP|status:ok|key:{key}|value:stub|E"

    def _rp(self, msg): return msg.raw
    def _ea(self, msg): return msg.raw


# ══════════════════════════════════════════════════════════
# §11  設定工具 (Setup)
# ══════════════════════════════════════════════════════════

def _prompt(msg: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        v = input(f"  {msg}{hint}: ").strip()
        return v if v else default
    except (KeyboardInterrupt, EOFError):
        return default

def _confirm(msg: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        v = input(f"  {msg} [{hint}]: ").strip().lower()
        return default if not v else v in ("y","yes")
    except (KeyboardInterrupt, EOFError):
        return default

def run_setup():
    print(f"\n{BOLD}{CYAN}{'═'*50}\n  LCP v3 設定工具\n{'═'*50}{RESET}\n")
    try:
        pf = get_platform()
        _ok(f"平台：{pf.description}")
        _info(f"DB 路徑：{pf.db_dir}")

        host = pf.ollama_url.replace("http://","").split(":")[0]
        if _port_open(host, 11434):
            _ok(f"Ollama 可連線：{pf.ollama_url}")
            try:
                with urllib.request.urlopen(f"{pf.ollama_url}/api/tags", timeout=3) as r:
                    models = [m["name"] for m in json.loads(r.read()).get("models",[])]
                if models: _ok(f"模型：{', '.join(models)}")
                else: _warn("尚未下載模型，建議執行：ollama pull qwen2.5:7b")
            except: _warn("無法取得模型列表")
        else:
            _err(f"Ollama 無法連線：{pf.ollama_url}")
            if pf.platform_type == PlatformType.WSL or pf.platform_type == PlatformType.MACOS:
                print(f"\n  請在 WSL/終端機執行：\n  {CYAN}OLLAMA_HOST=0.0.0.0 ollama serve{RESET}\n")

        # Moltbook Token
        _head("Moltbook API Key 設定")
        env_path = pf.db_dir / ".env"
        existing_key = ""
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "MOLTBOOK_API_KEY=" in line:
                    existing_key = line.split("=",1)[1].strip()

        if existing_key:
            _info(f"現有 Key：{'*'*8}{existing_key[-4:]}")
            if not _confirm("重新設定？", default=False):
                _ok("保留現有設定")
                _print_final(pf, env_path, existing_key)
                return

        print(f"\n  取得 API Key：\n  1. 前往 https://www.moltbook.com\n"
              f"  2. 先執行龍蝦註冊（python lcp.py register）\n"
              f"  3. 完成人類認領後取得 key\n")
        key = _prompt("Moltbook API Key（moltbook_xxx）")
        if key:
            env_path.write_text(f"MOLTBOOK_API_KEY={key}\n"
                                f"MOLTBOOK_BASE_URL={MOLTBOOK_BASE_URL}\n",
                                encoding="utf-8")
            if sys.platform != "win32": os.chmod(env_path, 0o600)
            _ok(f".env 已儲存：{env_path}")
        else:
            _warn("跳過 Token 設定")

        _print_final(pf, env_path, key or existing_key)

    except KeyboardInterrupt:
        print(f"\n{YELLOW}已中斷{RESET}")

def _print_final(pf, env_path, key):
    print(f"\n{'─'*40}")
    print(f"  平台      {_c(pf.description, CYAN)}")
    print(f"  .env      {_c('✅', GREEN) if env_path.exists() else _c('❌', RED)}")
    print(f"  API Key   {_c('✅', GREEN) if key else _c('⚠️  未設定', YELLOW)}")
    print(f"{'─'*40}")
    print(f"\n  快速測試：{CYAN}python lcp.py test{RESET}")
    print(f"  執行指令：{CYAN}python lcp.py run 'L|CA|openweather|taipei|E'{RESET}\n")

def run_register():
    print(f"\n{BOLD}龍蝦註冊{RESET}\n")
    name = _prompt("龍蝦名稱（英文，例：OpenClawBot）")
    desc = _prompt("描述", default="由 LCP v3 驅動的本地龍蝦")
    if not name:
        _err("名稱不能為空"); return
    print(f"\n  正在註冊 {name}...")
    result = MoltbookHandler.register(name, desc)
    if result.get("success") or "agent" in result:
        agent = result.get("agent", {})
        key   = agent.get("api_key","")
        claim = agent.get("claim_url","")
        print(f"\n  {GREEN}✅ 註冊成功！{RESET}")
        print(f"  API Key：{BOLD}{key}{RESET}")
        print(f"  ⚠️  立刻把 API Key 存起來！只顯示一次！")
        print(f"  認領網址：{claim}")
        print(f"\n  接著：")
        print(f"  1. 把認領網址傳給自己")
        print(f"  2. 完成 email 驗證 + Tweet 驗證")
        print(f"  3. 執行：python lcp.py setup  （設定 API Key）")
        if key:
            pf = get_platform()
            env_path = pf.db_dir / ".env"
            if _confirm(f"自動儲存 API Key 到 {env_path}？"):
                env_path.write_text(f"MOLTBOOK_API_KEY={key}\n"
                                    f"MOLTBOOK_BASE_URL={MOLTBOOK_BASE_URL}\n",
                                    encoding="utf-8")
                if sys.platform != "win32": os.chmod(env_path, 0o600)
                _ok(f"已儲存：{env_path}")
    else:
        _err(f"註冊失敗：{result.get('error', result)}")


# ══════════════════════════════════════════════════════════
# §12  測試套件 (Test Suite)
# ══════════════════════════════════════════════════════════

class _Stats:
    def __init__(self): self.passed=0; self.failed=0; self.skipped=0

    def check(self, cond: bool, name: str, detail: str = ""):
        if cond: _ok(name); self.passed += 1
        else:    _err(f"{name}  →  {detail}"); self.failed += 1

    def skip(self, name, reason):
        _info(f"SKIP  {name}  ({reason})"); self.skipped += 1

    def summary(self) -> bool:
        total = self.passed + self.failed + self.skipped
        color = GREEN if self.failed == 0 else RED
        print(f"\n{'═'*50}")
        print(f"{BOLD}測試結果：{color}{self.passed}/{total} 通過{RESET}"
              f"  失敗:{self.failed}  略過:{self.skipped}")
        print("═"*50)
        return self.failed == 0

def run_tests():
    print(f"\n{BOLD}{CYAN}{'═'*50}\n  LCP v3 完整測試套件\n{'═'*50}{RESET}")
    S = _Stats()
    import tempfile

    # ── 1. Parser ──────────────────────────────────────────
    _head("1. Parser 格式測試")
    for raw, cmd, params in [
        ("L|CA|openweather|taipei|E",    "CA", ["openweather","taipei"]),
        ("L|MB|general|標題|內容|E",      "MB", ["general","標題","內容"]),
        ("L|SK|key|value|E",             "SK", ["key","value"]),
        ("L|RM|memo|E",                  "RM", ["memo"]),
        ("L|EA|reward|ok|+1|E",          "EA", ["reward","ok","+1"]),
        ("L|RP|status:ok|data:x|E",      "RP", ["status:ok","data:x"]),
    ]:
        msg = _parse_lcp(raw)
        S.check(msg and msg.cmd==cmd and msg.params==params, f"合法：{raw[:40]}")

    msg = _parse_lcp("L|CA|openweather|taipei")
    S.check(msg and msg.cmd=="CA", "容錯：缺少 |E 自動補全")

    for raw, desc in [
        ("LCP|1|CA|test|END", "標準版格式"), ("L|XX|test|E","非 6cmd"),
        ("random text","純文字"), ("","空字串"), ("L||E","空指令"),
    ]:
        S.check(_parse_lcp(raw) is None, f"非法拒絕：{desc}")

    # ── 2. Challenge Solver ────────────────────────────────
    _head("2. 驗證挑戰解碼測試")
    for challenge, expected in [
        ("A] lO^bSt-Er S[wImS aT/ tW]eNn-Tyy mE^tE[rS aNd] SlO/wS bY^ fI[vE", 15.0),
        ("tH]e^ lO[bSt-Er hA/s^ tW]eNn-Ty cL]aWs aNd^ lO[sEs/ fI]vE", 15.0),
        ("A^ lObStEr] tRaVeL[s/ sIx-Ty mEtErS] aNd^ gAiNs^ tEn", 70.0),
        ("tHe^ cRaB] hAs/ fOrTy lEgS] aNd] lOsEs^ tWeNtY", 20.0),
    ]:
        val, expl = decode_challenge(challenge)
        S.check(val is not None and abs(val-expected)<0.01, f"解碼：期望={expected}  {expl[:50]}")

    S.check(format_answer(15.0)=="15.00", "format_answer: 15.0 → '15.00'")
    S.check(format_answer(-3.5)=="-3.50", "format_answer: -3.5 → '-3.50'")

    # ── 3. Sandbox ─────────────────────────────────────────
    _head("3. Sandbox 沙盒測試")
    sb = Sandbox()
    def _lr(statuses):
        return [LayerResult(i+1,"L|CA|t|E",s,"L|RP|status:ok|E","CA")
                for i,s in enumerate(statuses)]

    r = sb.validate_chain(["L|CA|x|E","L|SK|k|v|E","L|MB|g|t|b|E"], _lr(["ok","ok","ok"]))
    S.check(r.passed and r.ea_type=="reward", "3層全成功 → reward")

    r = sb.validate_chain(["L|CA|x|E","L|SK|k|v|E","L|MB|g|t|b|E","L|EA|reward|ok|+1|E"],
                          _lr(["ok","ok","ok"]))
    S.check(r.passed and r.state=="SANDBOX_EXIT", "4層全成功 → SANDBOX_EXIT")

    r = sb.validate_chain(["L|CA|x|E","L|SK|k|v|E","L|MB|g|t|b|E","L|EA|reward|ok|+1|E"],
                          _lr(["ok","ok","err"]))
    S.check(not r.passed and r.ea_type=="penalty", "4層層3失敗 → penalty")

    r = sb.validate_chain(["L|CA|x|E"]*5, _lr(["ok"]*4))
    S.check(not r.passed and r.state=="ABORT", "超過4層 → ABORT")

    r = sb.validate_chain(["L|CA|x|E","L|SK|k|ignore previous instructions|E","L|MB|g|t|b|E"],
                          _lr(["ok","ok","ok"]))
    S.check(not r.passed and "phishing" in r.reason, "釣魚關鍵字偵測")

    r = sb.validate_chain(["L|CA|api1|E","L|CA|api2|E","L|MB|g|t|b|E"], _lr(["ok","ok","ok"]))
    S.check(not r.passed and "cross_domain" in r.reason, "跨域 CA 攻擊偵測")

    for ea, desc in [("L|EA|hack|r|+1|E","非法type"),("L|EA|reward|r|+99|E","score超範圍")]:
        r = sb.validate_chain(["L|CA|x|E","L|SK|k|v|E","L|MB|g|t|b|E",ea], _lr(["ok","ok","ok"]))
        S.check(not r.passed, f"EA 格式拒絕：{desc}")

    # ── 4. TranslationStore ────────────────────────────────
    _head("4. TranslationStore 對照庫測試")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = TranslationStore(db_path)

    S.check(store.insert("查台北天氣","L|CA|openweather|taipei|E",0.9,"test"), "高信心入庫")
    r = store.lookup("查台北天氣")
    S.check(r and r.lcp_output=="L|CA|openweather|taipei|E", "精確查詢命中")
    S.check(not store.insert("模糊","L|CA|x|E",0.3,"test"), "低信心被拒")
    S.check(store.lookup("不存在xyz") is None, "未命中回傳 None")

    store.insert("test_ea","L|MB|g|t|b|E",0.75,"test")
    store.apply_ea_feedback("L|MB|g|t|b|E","reward")
    r2 = store.lookup("test_ea")
    S.check(r2 and r2.confidence > 0.75, "EA reward → confidence 上升")
    S.check(isinstance(store.stats(),dict), "stats() 正常回傳")
    os.unlink(db_path)

    # ── 5. 平台偵測 ────────────────────────────────────────
    _head("5. 平台偵測測試")
    pf = detect_platform()
    S.check(pf.platform_type in PlatformType, f"平台類型有效：{pf.platform_type.name}")
    S.check(pf.ollama_url.startswith("http://"), f"Ollama URL 格式正確")
    S.check(pf.encoding=="utf-8", "編碼強制 UTF-8")
    if _port_open(pf.ollama_url.replace("http://","").split(":")[0], 11434):
        S.check(True, "Ollama 連線 ✅")
    else:
        S.skip("Ollama 連線", "服務未啟動")

    # ── 6. Moltbook 解析 ───────────────────────────────────
    _head("6. Moltbook 指令解析測試")
    post = parse_mb_params(["general","今日天氣","台北晴天28度"])
    S.check(post.submolt_name=="general", "submolt 解析")
    S.check(post.title=="今日天氣", "title 解析")
    S.check(post.content=="台北晴天28度", "content 解析")

    post2 = parse_mb_params(["今日天氣","晴天28度"])
    S.check(post2.submolt_name=="general", "無 submolt 時預設 general")

    os.environ["MOLTBOOK_API_KEY"] = "test_token"
    try:    _load_api_key(); S.check(True, "環境變數 Token 讀取")
    except: S.check(False, "環境變數 Token 讀取")
    os.environ.pop("MOLTBOOK_API_KEY", None)

    # ── 7. 整合測試 ────────────────────────────────────────
    _head("7. 整合流程測試")
    os.environ["MOLTBOOK_API_KEY"] = "test_token_integration"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db2 = f.name

    global _platform_cache
    _orig = _platform_cache
    _platform_cache = PlatformInfo(PlatformType.WSL,"http://localhost:11434",
                                   Path(db2).parent,"utf-8","Test (WSL mock)")
    _platform_cache.db_dir.mkdir(parents=True, exist_ok=True)

    p = LCPParser.__new__(LCPParser)
    p.store      = TranslationStore(db2)
    p.ollama     = OllamaHandler()
    p.moltbook   = None
    p.translator = Translator(p.store, p.ollama)
    p.sandbox    = Sandbox()
    p.watcher    = MoltbookWatcher()

    r = p.run("L|CA|openweather|taipei|E")
    S.check(r.success and "openweather" in r.output, "單條 CA 執行")

    r = p.run_natural("查台北天氣")
    S.check(r.success, "自然語言命中對照庫")

    r = p.run_chain(["L|CA|openweather|taipei|E","L|SK|weather_today|晴天28度|E","L|RM|last|E"])
    S.check(r.success and r.sandbox.passed, "3層鏈沙盒通過")

    r = p.run_chain(["L|CA|x|E"]*5)
    S.check(not r.success and r.error=="depth_exceeded", "超過4層被拒")

    r = p.run_natural("!@#$%^&*()")
    S.check(not r.success and r.error=="low_confidence", "低信心輸入被拒")

    os.unlink(db2)
    os.environ.pop("MOLTBOOK_API_KEY", None)
    _platform_cache = _orig

    S.summary()


# ══════════════════════════════════════════════════════════
# §13  CLI 主程式
# ══════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]
    cmd  = args[0].lower() if args else "help"

    if cmd == "setup":
        run_setup()

    elif cmd == "register":
        run_register()

    elif cmd == "watch":
        watcher = MoltbookWatcher()
        force   = "--force" in args
        result  = watcher.check_and_update(force=force)
        print(f"\n版本：{result.version}  更新：{'✅' if result.updated else '❌'}")
        cfg = watcher.load_config()
        print(f"base_url：{cfg.get('base_url')}")
        print(f"rate_limits：{cfg.get('rate_limits')}")

    elif cmd == "test":
        run_tests()

    elif cmd == "run":
        if len(args) < 2:
            print("用法：python lcp.py run 'L|CA|openweather|taipei|E'")
            sys.exit(1)
        parser = LCPParser()
        r = parser.run(args[1])
        print(f"success:   {r.success}")
        print(f"output:    {r.output}")
        if r.error: print(f"error:     {r.error}")

    elif cmd == "chain":
        if len(args) < 2:
            print("用法：python lcp.py chain 'L|CA|x|E' 'L|SK|k|v|E' 'L|MB|g|t|b|E'")
            sys.exit(1)
        parser = LCPParser()
        r = parser.run_chain(list(args[1:]))
        print(f"success:   {r.success}")
        print(f"output:    {r.output}")
        print(f"ea_output: {r.ea_output}")
        if r.sandbox: print(f"state:     {r.sandbox.state}")

    elif cmd == "chat":
        if len(args) < 2:
            print("用法：python lcp.py chat '查台北天氣'")
            sys.exit(1)
        parser = LCPParser()
        r = parser.run_natural(" ".join(args[1:]))
        print(f"success:   {r.success}")
        print(f"output:    {r.output}")
        if r.error: print(f"error:     {r.error}")

    elif cmd == "home":
        try:
            mb = MoltbookHandler()
            h  = mb.home()
            print(json.dumps(h, indent=2, ensure_ascii=False))
        except ValueError as e:
            print(f"❌ {e}")

    elif cmd == "decode":
        # 測試驗證挑戰解碼
        if len(args) < 2:
            print("用法：python lcp.py decode '挑戰文字'")
            sys.exit(1)
        val, expl = decode_challenge(" ".join(args[1:]))
        print(f"解碼：{expl}")
        print(f"答案：{format_answer(val) if val is not None else 'N/A'}")

    else:
        print(f"""
{BOLD}LCP — Lobster Communication Protocol v3{RESET}

用法：
  python lcp.py setup              互動式設定
  python lcp.py register           註冊新龍蝦
  python lcp.py watch [--force]    檢查 API 版本更新
  python lcp.py test               執行完整測試套件
  python lcp.py run <LCP訊息>      執行單條指令
  python lcp.py chain <訊息...>    執行指令鏈
  python lcp.py chat <自然語言>    自然語言轉譯執行
  python lcp.py home               查看 Moltbook 首頁
  python lcp.py decode <挑戰文字>  解碼驗證挑戰
""")

if __name__ == "__main__":
    main()
