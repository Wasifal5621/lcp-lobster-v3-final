# LCP 開發紀錄 — 2026.03.18-19 完整摘要

## 本次對話完成的所有事項

### v3.1 混合模式 (Hybrid Mode)
- `OllamaHandler.lcp_to_natural()` — 7B 翻譯引擎
- `OutputMode` enum — lcp / natural / hybrid 三模式
- `run_hybrid()` — 內部 LCP 執行，最終輸出自然語言
- `run_hybrid_mb()` — MB 發文前自動翻譯成自然語言
- `_translate_output()` + `_fallback_translate()` — Ollama 在線用 AI 翻，離線做基礎解析
- CLI `hybrid` 指令 + `--mb` 旗標
- `ExecutionResult.natural_output` 新欄位

### v3.2 記憶庫持久化 (Memory Store)
- **§4b MemoryStore** — 全新 SQLite 記憶庫
- SK/RM 真正讀寫 SQLite（不再是 stub）
- **雙層存儲**：長文（>150字）自動 AI 摘要 + 原文保留
- `OllamaHandler.lcp_summarize()` — AI 摘要引擎
- **關鍵字搜尋**：搜 key/value/summary/tags 四欄位
- **自動相關性匹配** `_auto_context()`：執行 chain 時自動從記憶庫撈相關筆記（40x 壓縮）
- RM 特殊指令：`full:`、`search:`、`list:`、`delete:`、`stats`
- CLI `mem save/get/search/list/delete/stats`
- value 上限 512→4096 字元

### v3.3 記憶分層 (Memory Tiers)
- **三個分層**：core（永不過期）、daily（日常）、cache（暫存）
- `get_core_memories()` — 取得所有 core 記憶
- `export_core()` — 匯出核心記憶成 markdown（災難恢復）
- `cleanup_expired()` — 清理過期非核心記憶（core 永遠不刪）
- `_auto_context` 升級：core 永遠優先載入
- 搜尋排序：core 最前 → updated_at 降序 → access_count
- 分層統計：stats() 顯示 core/daily/cache/other
- CLI `mem export`、`mem cleanup`、`mem tier`

### v3.4 記憶圖譜 (Memory Graph)
- **lcp_memory_edges 表**：source/target/relation/weight
- `auto_link()` — 存入時自動建立關聯（same_tag + same_group）
- `link()` / `unlink()` — 手動建立/移除關聯
- `get_related(depth=N)` — 多層深度圖譜檢索
- `get_edges()` — 查看某筆記憶的所有邊
- `graph_stats()` — 圖譜統計
- `_auto_context` 升級：三步驟 = core + 搜尋 + graph 擴展
- RM 特殊指令：`graph:`、`link:`、`edges:`

### Bug 修復
- Windows SQLite 檔案鎖（加 `store.close()` + `try/except`）
- 垃圾輸入卡 Ollama（`len(cleaned) >= 2` 前置檢查）
- README CRLF 渲染異常（GitHub 網頁重建解法）
- `.gitattributes` 檔名錯誤（`gitattributes` → `.gitattributes`）

---

## 程式碼統計

| 版本 | 行數 | 測試 | 新增功能 |
|------|------|------|---------|
| v3.0（起點）| 1424 | 45 | 6指令集+沙盒+Moltbook |
| v3.1 | ~1600 | 53 | 混合模式 |
| v3.2 | ~2080 | 86 | 記憶庫+雙層存儲+自動匹配 |
| v3.3 | ~2165 | 95 | 記憶分層+時間衰減+核心匯出 |
| v3.4 | 2368 | 105 | 記憶圖譜+auto_link+depth檢索 |

---

## 檔案位置

- **GitHub repo**: https://github.com/vm6eji6m4/lcp-lobster-v3-final
- **本地路徑**: D:\OpenClaw_Scripts\LCP公告版
- **remote**: origin → lcp-lobster-v3-final
- **舊 repo**: lcp-lobster-v3 已刪除

---

## 三層壓縮理論

```
第一層：指令壓縮（自然語言 → LCP 格式）
  自然語言 ~100 token → LCP ~45 token = 2.2x

第二層：記憶壓縮（只載入相關記憶）
  100 筆記憶 20,000 字 → 5 筆摘要 500 字 = 40x

第三層：輸出分離（內部 LCP + 對外自然語言）
  Input → LCP(內部) → AI → 自然語言 Output(對外)

綜合效果：10-40x token 節省
```

---

## Roadmap 現況

### 已完成
- [x] v3.1 混合模式
- [x] v3.2 記憶庫持久化+雙層存儲+自動匹配
- [x] v3.3 記憶分層 core/daily/cache+時間衰減
- [x] v3.4 記憶圖譜 auto_link+depth 檢索
- [x] 版本相容（格式從未改變，天生向下相容）
- [x] 狀態恢復（core export + auto_context 重載）

### 未來方向
- [ ] Multi-lobster collaboration（需要社群第二隻 LCP 龍蝦）
- [ ] Semantic search（embedding 取代關鍵字）
- [ ] Memory compaction（合併相似記憶）
- [ ] Plugin system（社群自訂 CA handler）

### v3.5 規劃：金字塔壓縮（Golem 概念）
靈感來自 Golem 的金字塔壓縮架構：
```
Tier 0：原始日誌 → 72h 後 AI 壓縮成摘要
Tier 1：每日摘要 → 90天後壓縮成月度精華
Tier 2：月度精華 → 5年後壓縮成年度回顧
Tier 3：年度回顧 → 永久保留
Tier 4：紀元里程碑 → 永久保留

核心價值：不管用幾年，context 注入量永遠固定
LCP 現有的 cleanup 是「刪除」，金字塔是「壓縮」
結合方式：金字塔負責時間軸壓縮，LCP 圖譜負責語意關聯
```

實作要點：
1. MemoryStore 加 `tier` 欄位（0-4）
2. `compact()` 方法：定時觸發 LLM 壓縮
3. 壓縮時同步更新圖譜（抽取實體 → auto_link）
4. 啟動時注入順序：紀元→年度→月度→每日（context 預算固定）

---

## GitHub 注意事項

- README.md 不要從 Windows 本地 push（CRLF 渲染異常）
- 解法：GitHub 網頁上刪除 → Create new file → 貼上內容
- .gitattributes 強制 LF 但對中文混合內容可能無效
- .codegraph/ 資料夾被意外推上去了，下次加進 .gitignore

---

## Claude 記憶（8筆）

1. 硬體：MSI Stealth 16 AI Studio, RTX 4070 8GB, 96GB DDR5
2. Repo：lcp-lobster-v3-final, D:\OpenClaw_Scripts\LCP公告版
3. v3.4 功能摘要：2350行, 105測試
4. 版本歷程：v3.1→v3.2→v3.3→v3.4
5. GitHub vm6eji6m4, FB 193讚, Claude Pro
6. photo_tools.py 團體照工具
7. memory MCP + codegraph MCP 已安裝
8. README CRLF 注意事項

---

*這隻龍蝦，是我親手養大的。—— 國裕 2026.03.19* 🦞
