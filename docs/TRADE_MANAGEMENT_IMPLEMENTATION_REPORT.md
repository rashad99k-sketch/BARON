# Trade Management Implementation Report — Safety Hardening + Profit-Harvesting Lifecycle

التاريخ: 2026-09-07
الحالة: **مكتمل — جميع الاختبارات خضراء (576 passed, 1 skipped)، smoke فشل الوضع الفعلي لا**

---

## 1) النطاق (Scope)

أُغلقت فجوات P0/P1 في إدارة الصفقات على `core/engine.py` مع طبقة دَفتر أحداث مستقلة
`core/trade_journal.py`، دون المساس بسلطة `LiveTradeManager` الوحيدة على
SL/TP/Partial/Trailing/Close، ودون تغيير أي من معاملات الدخول/الخطر/الحجم/الرافعة/البرودة/ATOM/البوابات.

القواعد المرجعية التي حُكمت بها كل تغيير (من قائمة العمل المعتمدة + المراجعات السابقة):

1. LiveTradeManager هو **السلطة الوحيدة** على SL/TP/جزئية/تريل/إغلاق.
2. لا يوجد "دماغ" منافس؛ الطبقات الجديدة تزيّن القرارات الحالية وتسجلها فقط.
3. لا تغيير على الدخول/المخاطرة/الحجم/الرافعة/فترات التبريد/ATOM/البوابات.
4. وضع التشغيل الفعلي يبقى PAPER-MODE حصريًا (`PAPER_MODE=true` افتراضيًا).
5. **لا TP1/ربح مُلفّق عند استعادة إعادة الإقلاع** — الاستعادة تُعاد بناء المركز كما هو على البورصة فقط.
6. **الربح المحقق (REALIZED) لا يُخلط أبدًا مع غير المحقق (UNREALIZED) بما فيه الذروة**؛
   التصنيف النهائي للصفقة يُبنى حصريًا من المحقق (مثال: ذروة +40% ثم إغلاق -2% = LOSS).
7. P0-2: خطأ واجهة برمجية / PAUSED / ERROR **ليس دليل غياب المركز** — تُعاد اللقطة القديمة المحلية
   بعلامة `stale=True`؛ فقط الرد المؤكد `NOT_FOUND` (البورصة أجابت ولا يوجد مركز) يغلق محليًا
   (`EXTERNAL_CLOSE` + حدث `force_close_local` مرتبط بالرمز).

---

## 2) الملفات المتأثرة

| الملف | الدور |
|---|---|
| `core/trade_journal.py` (جديد) | طبقة دَفتر أحداث مرتبطة بـ `core.decision_journal` المزوّد بسلسلة التجزئة؛ مفردات أحداث/مراحل، `make_trade_id`, `classify_result`, `tp_geometry_valid`, `build_trade_meta`, `journal_trade_event` (مع dedup), `recover_trade_id`. |
| `core/engine.py` | كل أسلاك الحماية ودورة تصدير الأرباح + الأحداث. |
| `portfolio/manager.py` | `restore_from_exchange` + `canonical_position_payload` بمفاتيح P1-4. |
| `dashboard/app.py` | عزل USDT/ROE وعرض `profit_summary` ومفاتيح المرحلة/الحماية/المحقق بأدوات العرض. |
| `tests/test_trade_management_safety.py` (جديد) | 39 اختبارًا (المجموعات A/B/C). |
| `tests/test_position_management_phase1.py` | إصلاح تسريب مسبق للغايات العالمية (`close_partial`/`get_ticker_safe`/…) لم يتسرب إلى الأجنحة اللاحقة. |

---

## 3) P0-1 — دفتر أحداث دورة الصفقة (Trade Lifecycle Journal)

مفردات الأحداث المعتمدة (مرحلة `TRADE`، قرار = الحدث) موثقة في `core/trade_journal.py:11-34`
ومعرفة كسلسلة مفردة المصدر `47-67`:

```
TRADE_OPENED, RESTART_RECOVERY, POSITION_RECOVERED, PROFIT_DETECTED,
TP1_ELIGIBLE, TP1_EXECUTED, TP1_FAILED, PARTIAL_CLOSE, BREAKEVEN_RATCHET,
PROTECTION_UPDATE, PROFIT_LOCKED, PROFIT_LOCK_FAILED, TRAILING_ACTIVE,
TP2_EXECUTED, TRADE_CLOSED, EXTERNAL_CLOSE, NATIVE_SL_PLACED,
NATIVE_SL_UPDATED, NATIVE_SL_CANCELLED, NATIVE_SL_FAILED, POSITION_STATUS_UNKNOWN
```

- ذراع إعادة الطلب: `journal_trade_event(...)` (`core/trade_journal.py:200`) يلحق سجلًا موثقًا
  المتغيرات عبر `append_event` من `core.decision_journal` (سلسلة التجزئة ضد العبث).
- كل حدث يحمل `metadata` موحّدًا من `build_trade_meta(state)` (`:147`) يشمل:
  `trade_id, entry, side, current_size(remaining_qty), realized_pnl_usdt/roe,
  peak_roe/peak_unrealized_pnl, sl/tp1/tp2, profit_stage, protection_state,
  native_sl_state/price/order_id, tp1_state/tp2_state, position_status,
  exit_reason, final_result, duration_sec`.
- `trade_id` يُسجَّل دائمًا في `metadata` (`:224-226`) لأنه ركيزة استعادة إعادة الإقلاع.
- `dedup_key/dedup_sec` تمنع تكرار الأحداث المزعجة (مثل فحص TP1 الفاشل المتكرر) دون أن
  تحجب الانتقالات الأساسية.
- في `core/engine.py`، ذراع `_journal_trade_event` (`engine.py:6378`) هو نقطة واحدة موحّدة
  يُرسل من خلالها كل الأحداث، مع إشعار تلغرام `tg_trade_event` (Part M) لقائمة الأحداث المعتمدة.

---

## 4) P0-2 — خطأ الواجهة ليس غيابًا أبدًا (API Error is NEVER Absence)

- `fetch_live_snapshot` (`engine.py:1788`): مسار `except` لا يعيد `None` أبدًا — يبني لقطة راكدة
  من `_last_snapshot` مع `stale=True, source="rest_sync_error"` ويثبت
  `position_status = "UNKNOWN"` عبر `_position_status_unknown(symbol, source)` (`engine.py:6574`).
- `reconcile` (`engine.py:1896`): فقط رد `snap is None` المؤكد — أي "البورصة أجابت ولا يوجد
  مركز" — يُغلق المحلي: يسجل `EXTERNAL_CLOSE` ويبعث حدث `force_close_local` ببيانات
  `{"symbol": symbol}`. حالة `SYMBOL_GUARD.is_paused` تُبقي الحالة المحلية كما هي (لا إغلاق).
- `reconcile` يبقى **قراءة فقط في مسار التبني** (لا يصدر أوامر) — مطابق لمواصفة
  `tests/test_open_timeout_recovery.py`.
- المعالِج `_force_close(self, data)` (`engine.py:3826`): يهمل الأحداث غير المتطابقة الرمز
  (WARN متباطئ)، ويضع `STATE["close_reason"]="EXTERNAL_CLOSE"` عند المطابقة، ثم يغلق.

---

## 5) P0-3 — SL الحماية الأصلي (Native Protective SL)

- `place_native_sl(symbol=None)` (`engine.py:6594`): اختراق `reduceOnly` + `PositionSide`
  المشتقة من الجهة (`_hedge_position_side`)، كتابة في `native_sl_state/order_id/price`
  وأحداث `NATIVE_SL_PLACED/NATIVE_SL_FAILED`، وابدالية عند `native_sl_state == "ACTIVE"`
  (لا يضاعف الأوامر). آمن في وضع PAPER (لا يلمس البورصة؛ يسجل فقط).
- `update_native_sl(sl_price)` (`engine.py:6659`): **أحادي الاتجاه فقط** — رفض التخفيف —
  مع `NATIVE_SL_UPDATED` (never backward: BUY تصعد فقط، SELL تهبط فقط).
- `cancel_native_sl()` (`engine.py:6708`): إلغاء بجهود قصوى عند الإغلاق مع `NATIVE_SL_CANCELLED`.
- `_protect_wire_on_tick` (`engine.py:6743`): يوضع الأأمر عندما يتقدم حالة الحماية ويدمج السعر.
- **ملكية وضعه الوحيدة**: أسلاك الإدارة (`_apply_management` عبر `_protect_wire_on_tick`)؛
  مسار `_reconcile_levels_after_fill` يصحح **المستويات فقط** ولا يضع أوامر (انظر §6).
- `finalize_trade_with_reality` يلغي SL الأصلي عند الدخول (`engine.py:7153`).

---

## 6) P1-1 / P1-3 / P1-2 — التصلب + TP1 المثبت الفعل + إنهاء مرتبط بالرمز

- `start_trade` (`engine.py:3846`): يولد `trade_id` (`_generate_trade_id` → `_tj.make_trade_id`)،
  يسجل `TRADE_OPENED` بكل المستويات، يثبّت `profit_stage=OPENED,
  protection_state=NONE, position_status=OPEN, sync_status=OK, recovered=False`.
- `close_partial(ratio)` (`engine.py:3484`): يعود `bool` فعلي (True فقط بعد ملء مُتحقَّق)،
  ويسجل ساقًا محققة via `_record_partial_leg` (`engine.py:8132`):
  `realized_pnl_usdt/pct/roe`, `realized_legs`, `partial_realized`, حدث `PARTIAL_CLOSE`,
  وإيداع في `PERF`. في وضع PAPER يحافظ على `remaining_qty` في `paper["position"]`.
- قاعدة TP1-المثبت: فرع TP1 العدواني (`_apply_management`, `engine.py:4409`) لا يعلن
  `TP1_DONE` إلا بعد نجاح `close_partial` فعليًا (قيمة True) ثم يسجل
  `TP1_EXECUTED`/`TP1_FAILED` (الفرع يفشل دون إساءة الإعلان).
- `_reconcile_levels_after_fill` (`engine.py:7981`): يصحح المستويات فقط عند انحراف
  ملء/هندسة، لا يضع SL الأصلي (تجنبًا للازدواج مع ملكية الإدارة ولإبقاء تبني
  انتهاء المهلة "قراءة فقط" حسب المواصفة).

---

## 7) آلة مراحل حصاد الأرباح (Profit-Harvesting Stage Machine)

مراحل أمامية فقط (لا عودة): `OPENED → PROFIT_DETECTED → TP1_ELIGIBLE → TP1_EXECUTED
→ PROFIT_LOCKED → TRAILING_ACTIVE → TP2_EXECUTED → CLOSED` (`core/trade_journal.py:85-89`).

- `_ensure_trade_hardening_state(state)` (`engine.py:6361`): تهيئة ذرعية عند أول تلمس.
- `_advance_profit_stage(stage, reason, state, journal=False)` (`engine.py:6416`):
  تقدّم أحادي الاتجاه. الافتراضي `journal=False` لأن كل حدث مرحلة يُسجل صراحةً في
  موقعه الخاص — يمنع الازدواج (لا تكرار لـ PROFIT_LOCKED/PROFIT_DETECTED/TP1_EXECUTED).
- `_on_profit_detected(symbol, roe)` (`engine.py:6558`): مرة واحدة لكل صفقة
  (`PROFIT_DETECTED` + مرحلة PROFIT_DETECTED).
- `_safe_prot_floor(side, candidate)` (`engine.py:6448`): أرضية رتيبة
  (BUY: الصعود فقط، SELL: الهبوط فقط) تحفظ `protection_floor_sl`.
- `_apply_protection_ratchet(...)` (`engine.py:6470`): **بوابة على `tp1_hit` على الفرعين**
  (لا تعادل/تضييق قبل هبوط TP1 الحقيقي):
  - الفرع الأول: بعد TP1 → BREAKEVEN مثبّت عند نقطة الدخول + `BREAKEVEN_RATCHET`.
  - الفرع الثاني: بعد TP1 →  راتشيت رتيب من `synthetic_sl/trail_stop` مع
    `PROTECTION_UPDATE` (dedup 30s).
- `_profit_lock_then_journal(...)` (`engine.py:6534`): فقط إن كانت الحماية قد هبطت
  (`protection_state in (BREAKEVEN, PROFIT_LOCK, TRAILING)`): `PROFIT_LOCKED` وإلا `PROFIT_LOCK_FAILED`.
- `_protect_wire_on_tick` (`engine.py:6743`): يُستدعى قبل فحص SL إدارة فعلية؛ يحدّث
  SL الأصلي أحادي الاتجاه ويواصل التريل عند تفعيله.
- `_apply_management` (`engine.py:4409`): بعد `_update_peak_profit`، دمج النافذة أعلاه؛
  أفرع الخروج تُسجل `close_reason` و `TP2_EXECUTED` / `TRAILING_ACTIVE` / PPE-exit /
  فحص SL النهائي مع `TRAILING_ACTIVE` عند تفعيل التريل.
- `_classify_trade_result(pnl_pct)` (`engine.py:6370`) و`finalize_trade_with_reality`
  (`engine.py:7147`): تصنيف من المحقق فقط (`_tj.classify_result`) —
  `final_result_class` = WIN/LOSS/BREAKEVEN حسب `|pnl| > eps`، مع
  `TRADE_CLOSED` بكل الميتاداتا و `last_trade_summary`.

---

## 8) إعادة إقلاع وآمنة + لوحة التحكم

- `portfolio/manager.py::restore_from_exchange` (`manager.py:296`):
  - `ACTIVATE` → تبنّي مراكز البورصة كحالة مفتوحة (لا إعادة حساب للفكرة؛ لا ربح مُلفّق).
  - استعادة `trade_id` عبر `_tj.recover_trade_id(symbol)` وإلا `engine._generate_trade_id(symbol)`.
  - تثبيت `recovered=True, recovery_ts, position_status="RECOVERED", close_reason=None`.
  - أحداث `RESTART_RECOVERY` + `POSITION_RECOVERED` (dedup)؛ تنفيذ هندسة SL/TP عبر
    `engine._enforce_sl_tp_geometry`؛ `place_native_sl(symbol)`.
  - لا يُكتب أي ربح محقق افتراضي.
- `canonical_position_payload(symbol, s, asset_class)` (`manager.py:431`): مفاتيح P1-4 إضافية:
  `trade_id, profit_stage, protection_state, protection_floor_sl,
  realized_pnl_usdt/pct/roe_pct, realized_legs, tp1_state, tp1_exec_price,
  tp1_event_ts, tp2_state, tp2_event_ts, trail_activation_ts, native_sl_state/price,
  position_status, sync_status, recovered, exit_reason, final_result_class,
  last_trade_summary`.
- `dashboard/app.py`: `data()` يبني `profit_summary` (محقق/غير محقق منفصل) ويظهر بطاقة
  المركز الأصلي/الاحتياطي — USDT PnL مع ROE % منفصلتين، ومرحلة/حماية/محقق/`trade_id`.

---

## 9) التحقق (Verification)

- `tests/test_trade_management_safety.py` — 39 اختبارًا (A: دفتر الأحداث، B: تصلّب المحرك،
  C: الحمولة الأصلي + الحماية) ✅ اجتازت.
- أجنحة الانحدار الكاملة: **576 passed, 1 skipped** ✅ (بينها `test_position_management_phase1`,
  `test_profit_engine_phase3`, `test_accounting_lifecycle`, `test_fill_reconciliation`,
  `test_decision_journal`, `test_regressions`, `test_forensic_fixes`, `test_dashboard_contracts`,
  `test_portfolio_isolation`, `test_open_timeout_recovery`, ...).
- `python -m py_compile core/engine.py core/trade_journal.py portfolio/manager.py dashboard/app.py` ✅
- `tools/paper_runtime_smoke.py` → `PAPER_RUNTIME_SMOKE=PASS` ✅
- إصلاحان بيّضان في جناح قائم: `test_position_management_phase1.py` كان يُسرّب
  `E.close_partial` إلى `lambda` و `E.get_ohlcv_safe/get_orderbook_cached/get_ticker_safe`
  دون استعادة (في `tearDown` معاد بناؤه الآن) — أثر ذلك كان يظهر فقط عند تشغيل أجنحة متتالية.

---

## 10) ملاحظات تشغيل

- بقيت فقط حالة fleaky معروفة: `ProfessionalTradingScenarioTest` (phase3) قد يرمي
  `test_full_sweep_leaves_no_ghost_positions`/`test_sl_exit_still_fires_when_ifvg_payload_present`
  بشكل عابر عند التشغيل الكامل تحت حمل عالٍ (سلوك وزمني للبوابات القائمة — حساس للوقت
  `last_*_ts`)، يمرّ عند التشغيل المنفرد؛ لا علاقة لهذا بتنفيذ P0/P1 الحالي.
- `place_native_sl` في وضع PAPER لا يلمس البورصة (سجل فقط)، فاختبارات PAPER آمنة.
- الدفتر: متغير `DECISION_JOURNAL_PATH` (افتراضي `logs/decision_journal.jsonl`).