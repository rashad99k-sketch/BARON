# تقرير المراجعة الجنائية — منظومة Profit Taking + Dynamic Position Management

التاريخ: 2026-08-29
الحالة: **قيد الانتظار — لا تعديل لأي ملف إنتاجي قبل موافقة المستخدم**

---

## 1) نطاق المراجعة والمنهجية

رُوجع الكود كاملًا قبل أي تغيير، وفق مبدأ "ابحث عن النظام الموجود وأعد استخدامه قبل بناء أي نسخة مكررة".
الملفات التي فُحصت فعليًا (قراءة/بحث سطري):

- `core/engine.py` (11,704 سطرًا) — قلب التنفيذ والإدارة.
- `portfolio/manager.py` (329) — تنسيقة المحفظة وعزل المركز لكل رمز.
- `portfolio/allocator.py` — قبعة الفئات والجهات وتفسير كل رفض.
- `portfolio/risk.py` — حارس المخاطر (خسارة يومية/تتابع/هامش/فترات تبريد).
- `portfolio/news_slot.py` (164) — الفتحة الخبرية المستقلة (opt-in).
- `news/service.py` — طبقة الأخبار (RSS + Yahoo) وتدفق `news_risk` داخل `MEMORY["watchlist"]`.
- `core/runtime.py` — `portfolio_loop`، بوابات التنفيذ، منفّذ فتحة الأخبار.
- `strategy/engine.py`, `config/settings.py`, `app/bootstrap.py`, `dashboard/app.py`.
- الاختبارات الموجودة: `tests/test_position_management_phase1.py`، `tests/test_regressions.py`، `tests/test_runtime_repairs.py`، وأجنحة المحفظة.

النتيجة المركزية: **النظام المطلوب موجود فعليًا في `core/engine.py` على طبقتين (Advisory + Enforceable) مع Health Score، Classification، Trailing، وTerminal Distribution**؛ والمرحلة القادمة (Phase 3) هي **سّد فجوات محددة** لا إعادة بناء.

---

## 2) التدفق الحالي (Current Flow) — حرفيًا من الدوال والأسطر

### مسار الدخول (Entry)
1. `core/runtime.py` → `portfolio/allocator.py` → `PortfolioManager.open_candidate(candidate)` (`portfolio/manager.py:156`).
2. `open_candidate` → `activate(symbol)` → `engine.execute_entry(...)` (`engine.py:5998`).
3. داخل `execute_entry`:
   - بوابة ADX الصلبة `[25, 38]`؛ بوابة **Liquidity Sweep** (BUY لابد `sell_side_taken` / SELL لابد `buy_side_taken`).
   - الحجم = 10% من الرصيد الحر × `BALANCE_SAFETY_FACTOR` (= بند `POSITION_MARGIN_PCT`).
   - في وضع PAPER: تحديث `paper["position"]` وكتابة مفاتيح الحالة: `tp1/tp2/sl`, `trade_type`, `entry_type`, `trade_thesis`, `market_regime`, `adx_live`, `di_plus_live`, `entry_atr`.
4. عند النجاح → `_store_after_open(symbol, self.engine._live_manager, asset_class)` (`manager.py:173`) — هكذا يتحقق **عزل 6 مراكز**: لكل مركز نسخة خاصة من `STATE/TRADE_STATE` + `LiveTradeManager` مستقل.

### دورة الإدارة لكل مركز (Management loop)
`runtime.portfolio_loop` → `PortfolioManager.manage_all()` (`manager.py:200`)، لكل رمز:

1. `activate(symbol)` → إعادة تحميل حالة المركز وحقن `_live_manager` الخاص به.
2. `sync_position_state(symbol)` (`engine.py:4049`).
3. `_live_manager.manage_live_trade()` (`engine.py:3632`) → `_apply_management(symbol, now)` (`engine.py:3683`). ترتيب التنفيذ داخل الدورة:
   - الحواجز الصلبة أولًا (SL صلب بعلامة `[HARD_EXIT]`، profit lock، TP1، TP2).
   - **Phase 1 (Advisory فقط — بدون آثار جانبية):** `_run_advisory_health(...)` (`engine.py:3343`) → يحسب `PositionHealthScore` + `DynamicPositionProfile` ويخزن `position_trade_type / position_asset_class / position_confidence` ويصدر سجل `[POSITION]` الكامل عبر `log_position_decision` (`engine.py:2468`).
   - `apply_50_50_profit_engine(...)` (PPE) (`engine.py:4010` → التعريف `3121`): Stage1 BE عند 1.2×ATR بعد أدلة استمرار؛ Stage3 Runner عند ADX≥25؛ تحديث `trail_stop` بـ `max_price − mult×ATR`؛ خروج على trail / ADX متناقص + شمعة معاكسة / BOS هيكلي / greed profit lock؛ تشديد trail عند climax.
   - **Phase 2 (قابل للتنفيذ):** `_apply_dynamic_profit_and_exit_rules(...)` (`engine.py:3410`):
     - انعكاس مؤكد (REVERSAL) → **خروج كامل** بـ `close_position_full`.
     - انعكاس متوسط → جزئي + Runner + حماية.
     - Exhaustion → رفع SL إلى نقطة التعادل (protect).
     - هدف ربح مبكر مبني على ATR/فئة الأصل (`roe_target = tp1_atr * (atr/entry) * 100`, `engine.py:3500`) لصفقات REVERSAL/الأصول الضيقة.
   - منطق TP1/TP2 والتريل (كتلة `_apply_management` و`_update_peak_profit` `engine.py:3620`).
4. **حارس ختامي حقيقي** خارج مدير المركز: `council_exit(df, price)` (`manager.py:218` → `engine.py:7335`): خروج إذا ADX<18 أو مسّ SL الاصطناعي → ثم `finalize_trade_with_reality(symbol)` (`engine.py:5434`).
5. إن أُغلق المركز → إزالة من `contexts`؛ وإلا `_capture()` لتجميد الحالة؛ ثم `deactivate()`.

### ملاحظات تحكم حاسمة
- `USE_PPE=True` (`engine.py:293`، قابل للتعديل من البيئة) — PPE يعمل **بعد** الدماغ والتريل: ما ينتهي عليه فعليًا هو آخر كتلة أُجريت. يجب تثبيت هذا السلوك بالاختبارات.
- فترة الدورة: `target_interval` = 5 ثوانٍ هادئة / 2 ثانية نشطة، والمزامنة مع البورصة كل 10 ثوانٍ.

---

## 3) المكونات الموجودة — إعادة استخدام مباشرة (REUSE)

| المكوّن | الموقع | الدور |
|---|---|---|
| `AssetBehaviorProfile` | `engine.py:2207` + جدول `2234` | تكييف حسب فئة الأصل: CRYPTO/INDEX/STOCK/GOLD/OIL/NEWS — `tp1_atr, tp2_atr, sl_mult, trail_mult, roe_trail_activate, runner_bias` (كله قابل للتعديل من البيئة). |
| `PositionHealthScore` | `engine.py:2375` | 7 مكوّنات مرجّحة: trend .25 / structure .15 / liquidity .15 / momentum .15 / institutional .10 / zone .10 / risk .10 → `HOLD/HOLD_TRAIL/PROTECT_PROFIT/PARTIAL/EXIT` + confidence. الأخبار تخفّض مكوّن risk فقط (لا إغلاق أعمى). |
| `DynamicPositionProfile` | `engine.py:2273` + `update` `2321` | تصنيف ديناميكي `TRADE_TYPE ∈ {TREND, REVERSAL, PULLBACK, RETEST, BREAKOUT}` من `trade_state/trend_health/structure` بدون `trend=true` ثابت. |
| `MarketRegimeClassifier` | `engine.py:1033` | `MARKET_REGIME ∈ {TRENDING, EXPANSION, CORRECTION, RANGE, EXHAUSTION, REVERSAL_RISK}`. |
| `TradeStateMachine` + `InstitutionalTrendEngine` + `InstitutionalTradeBrain` | `engine.py:2537/2658/2893` | حالة الوزن، ترند مؤسسي، دماغ إدارة (تأخير TP1، trail multiplier، حماية، انعكاس). |
| `apply_50_50_profit_engine` (PPE) | `engine.py:3121` | BE / Runner / Trail / خروج BOS / profit lock / تشديد climax. |
| Phase-1 advisory layer | `engine.py:3343` | [POSITION] سجل غني + حالة التصنيف + Health (بدون آثار جانبية). |
| Phase-2 enforceable rules | `engine.py:3410` | خروج انعكاس / حماية exhaustion / جزئي ATR. |
| `detect_liquidity_context` | `engine.py:4668` | سياق السيولة (يُستخدم عند الدخول). |
| طابور/تمييز الذكاء المؤسسي | `engine.py:1608/1707/1780` | SmartMoney/Momentum/Institutional Intent. |
| أخبار | `news/service.py` + `deep_scanner.py:780` + `runtime.py:266` + `_get_advisory_news_state` (`engine.py:2492`) | `news_risk` يُكتب في الـ watchlist → حجب الدخول عند ≥ `NEWS_RISK_BLOCK` → تخفيف الـ health. |
| فتحة أخبار مستقلة | `portfolio/news_slot.py` | opt-in عبر `NEWS_SLOT_ENABLED=True`؛ عكس غير محسوب على قبعة الفئات؛ مقاس ATR (sl 1.5× / tp1 2× / tp2 3×). |
| عزل 6 مراكز | `portfolio/manager.py` | `PositionContext` لكل رمز: state/trade_state/live_manager/paper_position/asset_class. |
| تشخيص الرفض | `portfolio/allocator.py` | `SLOT_CAP` ثم قبعة الفئة (`CRYPTO_CAP`…) ثم قبعة الجهة (`BUY_CAP`/`SELL_CAP`) (الجهة تعلو الفئة عند التصادم — توثّق وتُثبَّت بالاختبار). |
| حارس المخاطر | `portfolio/risk.py` | `MAX_DAILY_LOSS_PCT=5.0`, `MAX_CONSECUTIVE_LOSSES=3`, `POSITION_MARGIN_PCT=0.10`, `PORTFOLIO_MARGIN_CAP_PCT=0.60`, فترات تبريد. |
| الاختبارات الحالية | `tests/test_position_management_phase1.py` | وحدة التصنيف/الصحة + قواعد المرحلتين + تخفيف الأخبار. |
| اختبار الإثبات الجديد | `tests/test_portfolio_dynamic_6way.py` | دورة حياة 6 مراكز + تفسير الرفض (يمر الشامل بنجاح). |

---

## 4) كود ميت/مكرر داخل engine (تُوثَّق ولا تُلمس)

- `DynamicTradeManager` (`engine.py:2052`): يُنشأ ويُخزَّن في `STATE["dynamic_manager"]` عند الدخول (`6171/6245`) لكن `update()` **لا يُستدعى أبدًا** ← حاوية ميتة.
- `apply_profit_engine` (`engine.py:4592`): **لا يُستدعى أبدًا**.
- `update_trailing_simple` / `stop_hit` / `manage_take_profit` / `scaling_logic` / `trailing_stop_new` (`engine.py:7245–7391`): دالة الإدارة القديمة أحادية المركز؛ تستخدم فقط في `engine.py:9190–9197` (الحلقة القديمة للرمز الواحد) — **خارج مسار المحفظة الحي**.
- `runner_bias`: معرّف في جدول `AssetBehaviorProfile` (L2219–2230) لكن **غير مستهلك في أي منطق** — فجوة حقيقية (انظر الفجوات G3) وليست تكرارًا.

---

## 5) تعيين المتطلبات الـ17 → الحالة الحالية

| # | المتطلب | الحالة | ملاحظة/المرجع |
|---|---|---|---|
| 1 | تصنيف ديناميكي (TRADE_TYPE / MARKET_REGIME) بدون `trend=true` | ✅ موجود | `DynamicPositionProfile` + `MarketRegimeClassifier`؛ يوجد ثبت في state. |
| 2 | Profit Taking ديناميكي (ATR/هيكل/زخم/سيولة/VWAP/OB/ADX/RSI/MACD/BOS…) | 🟡 جزئي | ATR/BOS/زخم/سيولة/ADX مفعّلة في الإدارة؛ **VWAP وRSI/Stoch وMACD وOB distance وVolume غير موصولة بمسار الإدارة** (تُحسب في أماكن أخرى ولا تدخل الحالة). |
| 3 | State Machine بأقل حالات | ✅ موجود | تصنيف `trade_state_class` + `TradeStateMachine`؛ مخزّن لكل مركز. |
| 4 | Health Score (7 مكوّنات) | ✅ موجود | `PositionHealthScore` + [POSITION] log. |
| 5 | Pullback vs Reversal | ✅ موجود | `analyze_pullback` (WEAK_PULLBACK) + `HEALTHY_PULLBACK` + `structure_aligned` + قواعد انعكاس Phase-2. |
| 6 | تكييف حسب الأصل | ✅ موجود | `AssetBehaviorProfile` يُستخدم في SL/trail/roe/TP-partial. |
| 7 | News-aware بدون إغلاق أعمى | 🟡 جزئي | تخفيف risk فقط؛ **لا يوجد معالجة وفق اتجاه الخبر/تأثيره/ردة فعل السعر/تمدد الفولاتيليتي**. |
| 8 | SL ديناميكي (breakeven→profit lock→structure/ATR trailing) | ✅ موجود | `SL_FIXED`، ratchet الـ dist_risk (1.6/1.2/0.8)، BE عند TP1، trail ATR، exit على انعكاس. |
| 9 | Runner عند ترند صحي | ✅ موجود | PPE Stage3 (ADX≥25) + دفاع runner + TP2 ديناميكي 5%/8% + runner_bias (**غير مستخدم** → G3). |
| 10 | لا Scalping عرضي | ✅ موجود | `target_interval` (5/2 ث) + `tp1_hold_score` يؤخّر TP1 + الحفاظ على حامي الربح. |
| 11 | تسجيل مفصل `[POSITION]` | ✅ موجود | `log_position_decision` (symbol/qty/side/entry/mark/roe/atr/health/action/reason/confidence…). |
| 12 | اختبارات (وحدة/تكامل/state/دورة/بيانات حقيقية/PAPER/انحدار) | 🟡 جزئي | موجود لـ Phase-1 والإثبات؛ **خطة 18 سيناريو تسد الفجوات** (انظر §9). |
| 13 | Six-position stress (2 crypto/2 index/1 gold/1 oil + news slot مستقل) | 🟡 مفعّل جزئيًا | بنية العزل موجودة والاختبار الجديد يغطي (BTC/ETH + US500/USTECH + XAU + WTI)؛ **اختبار يضم فتحة NEWS مُفعّلة لم يُبنَ بعد**. |
| 14 | واقعية (لا fake success/لا تجاوز بوابات) | ✅ التزام | البوابات الحقيقية تعمل في الاختبار (ADX + liquidity sweep فعلية)؛ نثبّتها ولا نعطّلها. |
| 15 | Performance (لا API spam) | ✅ موجود | `get_ohlcv_safe` cache، حساب ثقيل كل 5 ث، مزامنة كل 10 ث، `rate_limit()`. |
| 16 | معايير القبول الـ14 | 🟡 | خريطة القبول في §10 — العنصر 9 (ختم السجل الوقتي للأخبار) هو الوحيد غير المدعوم بوسم زمني قابل للفحص حاليًا. |
| 17 | مراجعة ثم توقف للموافقة | ▶️ الآن | هذا التقرير هو التوقف قبل أي تعديل. |
| 18 | **IFVG — نمط تحذيري داخل الـ Waiting/Institutional Analysis** (ورد طلبًا بعد §11) | 🟡 جديد | لا يوجد دعم حالي: `detect_fvg` (`engine.py:6324`) و`_fvg_context` (`deep_scanner.py:519`) يعالجان FVG كلحظة واحدة (آخر فجوة) وكإشارة موجبة فقط داخل الـ 0.5% — **لا تتبع حالة (NORMAL→MITIGATED→INVALIDATED→INVERSE)** ولا تطرح ثقة الـ OB/Zone المتعارض ولا تميز Retest من الاختراق. → التفصيل الكامل في **§11** (G7). |

---

## 6) الفجوات الحقيقية المعرَّفة بدقة (Phase-3 Scope)

فرزت الفجوات بالأدلة السطرية — هذه وحدها ما سيُنفَّذ بعد الموافقة، وكلها **إضافات** لا تغيير جوهرة التدفق:

1. **G1 — أهداف ربح ATR ديناميكية للمسار الرئيسي:** TP1=4% وTP2=5%/8% حاليًا ثابتة لصفقات TREND (`execute_entry`). الهدف المُعتمد على ATR (`roe_target` في `engine.py:3500`) يخدم REVERSAL/الأصول الضيقة فقط. → زم Wiring لكي يصبح TP ديناميكيًا لكل أنواع الصفقات والأصول.
2. **G2 — إشارات مفقودة من مسار الإدارة:** VWAP distance، RSI/StochRSI، MACD، Volume، OB/Zone distance (خصوصًا `zone_strength_score` يُقرأ بـ0.0 في `engine.py:3360` ولا يُمرَّر من OB حيّ في الإدارة)، Volatility regime. الدوال المحسبة موجودة في مكان آخر وتُمرَّر إلى `PositionHealthScore` و`DynamicPositionProfile` وPhase-2 فقط.
3. **G3 — `runner_bias` عاطل:** معرّف لكن غير مستهلك؛ سيُفعل كمعامل "عدوانية الرانر" لكل فئة أصل.
4. **G4 — إدارة الأخبار اتجاهًا وتأثيرًا:** إضافة مستوى `NEWS_LOW/MEDIUM/HIGH/CRITICAL` + اتجاه الخبر/تأثيره + فحص ردة فعل السعر وتمدد الفولاتيليتي ومواءمة الاتجاه — مع الإبقاء على قاعدة "الخبر لا يُغلق أبدًا بلا دليل".
5. **G5 — تكييف الفولاتيليتي:** فترات/تريل تتلاءم مع نظام التذبذب (volatility regime) وليست ثابتة على roe/adx.
6. **G6 — صلابة Windows:** `log_execution` يرمي `UnicodeEncodeError` (cp1252) عند طباعة `\u2011`/الإيموجي في وحدة التحكم — إصلاح غير سلوكي ويمنع تعطّل الاختبارات (عطل سابق الوجود أثبته `ExecutionEntryRealTest`).
7. **G7 — محرّك IFVG (طلب مضافة):** كما حدّده المستخدم — نمط تحذيري لا مؤشر إضافي: تتبع دورة حياة كل FVG حتى الانعكاس، تحذير من الدخول داخل منطقة IFVG، تخفيض ثقة OB/Zone المتعارض، مراقبة الـ Retest وتمييز الرفض الحقيقي من الاختراق المؤقت، التفرقة بين Pullback وStructure-change عند وجود مركز مفتوح، وتجميع IFVG + MSS/CHoCH/BOS عكسي + ضعف زخم/سيولة → رفع احتمال الـ Reversal — مع قاعدة "السعر يحترم الترند وIFVG مجرد Retest → HOLD وليس Exit". يُبنى كمكوّن إضافي يُستهلك من الأنظمة الحالية وليس مؤشرًا مستقلاً معقّدًا. التفصيل الكامل في **§11**.

---

## 7) الملفات والدوال التي ستتغير (نظريًا — بانتظار الموافقة)

> المرحلة التنفيذية ستُكمل هذا الجدول بعد الموافقة على خطة التنفيذ التفصيلية.

**إضافة/توسيع داخل `core/engine.py`:**
- `_apply_dynamic_profit_and_exit_rules` (`:3410`): G1/G2 (TP ديناميكي + إشارات VWAP/RSI/MACD/OB/Volume)، G4 (قرارات الأخبار المبنية على اتجاه/ردة فعل)، G7 (احتمال الـ Reversal + قاعدة HOLD عند Retest بنمط IFVG)، مع بقاء الفصل الصارم Advisory(بلا أثر) / Enforceable.
- `_run_advisory_health` (`:3343`): تمرير إشارات G2 وG7 إلى الـ health والتشخيص وأسباب القرار.
- `AssetBehaviorProfile` (`:2207`): تفعيل `runner_bias` (G3) وإضافة معاملات فولاتيليتي (G5).
- `log_execution` (`~:140`?): صيانة cp1252 (G6). يُحدَّد الموقع القياسي أثناء التنفيذ.
- دالة مساعدة جديدة (مقترحة، مكانها الطبيعي engine مسار Phase-2): محرّك "Trailing Exit Logic" موحّد يُستهلك من كتلة `_apply_management` الحالية بدل التشتت.
- **G7 — IFVG Detection Engine (جديدة في engine، بجوار `detect_fvg`/`get_smart_zones`):** `detect_fvg_lifecycle(df, lookback)` + `ifvg_warning_payload(symbol, side, df, atr, entry_price)`. تفصيل §11.

**ملفات أخرى:**
- `scanner/deep_scanner.py`: توسيع `_fvg_context` (L519) إلى سياق دورة الحياة + خصم درجة الـ watch_score عند IFVG معارضة (L594-596) وإضافة سبب تحذيري في القائمة (L612-620) — المرحلة STRONG نفسها يبقى بها الرمز فقط إذا صمدت نقاطه بعد الخصم.
- `scanner/scanner.py`: داخل `check_institutional_entry` (L266): بعد التحقق من الـ zone_ok (L283-317) فحص `ifvg_warning_payload`؛ IFVG عائق مقابل الدخول → خصم/إجهاض إلا إذا أغلقت شمعة Displacement قوية **عبر** المنطقة (تحييد الـ IFVG في اتجاه الصفقة). الداخلية موجودة: `detect_displacement` (L335)، `detect_bos/choch` (L320-328).
- `core/engine.py` قائمة الانتظار المؤسسية: `ZoneMetrics` (~L9930): حقول جديدة `ifvg_penalty: float = 0.0`, `ifvg_warning: str = "CLEAR"`, ووزن جديد داخل `final_zone_score` (L9963-9978، مثل `ifvg_alignment: 0.07`) + تمريرها في `ExecutionCandidate.to_dict` (L10045) — فيصبح **Final Opportunity Score** يتضمن IFVG صراحةً فلا يخفي OB الممتاز IFVG عكسية قوية أمام الدخول.
- `portfolio/manager.py`: لا تتغير إلا إذا دعت الحاجة؛ `portfolio/news_slot.py`: لا تتغير (إعادة استخدام)؛ `config/settings.py`: + مفاتيح G1/G3/G5/G7 (`IFVG_ENABLED=True`, `IFVG_LOOKBACK`, `IFVG_PENALTY_MAX=0.30`, `IFVG_BLOCK_DISTANCE_ATR`...)؛ `tests/*.py`: أجنحة G7 جديدة (unit/lifecycle/إجهاد).
- إصلاح سطري أساسي لـ `detect_fvg` (`engine.py:6324`): تحريكه إلى المدى (`detect_fvg_lifecycle`) مع الإبقاء على واجهته القديمة (متوافق — الاختبارات والأماكن الأخرى لا تنكسر).

**قرار صريح يُحترم:** لا يُلمس أي من الكود الميت في §4.

---

## 8) المخاطر

| R | المخاطرة | مستوى | التخفيف |
|---|---|---|---|
| R1 | تداخل PPE مع دماغ التريل (كلاهما يعدّل trail/جزئي) | مرتفع | تثبيت الترتيب الحالي بالاختبار؛ إضافة سجل "العامل الأخير" لكل قررار. |
| R2 | أهداف ATR قد تفتح أهدافًا أقرب من سقف الفائدة لأصول سريعة | متوسط | حدود دنيا/قصوى لكل فئة أصل؛ اختبارات الحدود. |
| R3 | إضافة إشارات قد تزيد حِسابات الدورة | منخفض | إعادة استخدام الدوال المحسوبة؛ تحديث state بدل حساب مزدوج. |
| R4 | تغيير Phase-2 قد يمسّ دخولًا/خروجًا حقيقيًا | متوسط | كل الفجوات تُنفَّذ داخل الـ Enforceable/Advisory بعلَم `USE_PHASE3` (افتراضي ON في الاختبار، قابل للإيقاف) — لا تغيير في `council_exit` ولا في `execute_entry` الأساسي. |
| R5 | أخبار التوجيه قد تسبب إغلاقًا مفرطًا | متوسط | قاعدة صارمة: لا إغلاق بدون دليل صحّي؛ الخبر يغير الاستحقاق/الـ health فقط. |
| R6 | الانحدار (141 اختبارًا كاملًا) | مرتفع | بوابة اختيارية: لا commit/push قبل أن تعبر الأجنحة الجديدة + أجنحة المحفظة + no-new-error على الدفعة الكاملة. |
| R7 | G7/IFVG: إنذارات كاذبة أو خصم مفرط يحجب فرصًا حقيقية | متوسط | حد أقصى للخصم (`IFVG_PENALTY_MAX=0.30`)؛ لا يُجهض الدخول إلا إذا كانت IFVG **فعلًا** في مسار الدخول (ضمن `IFVG_BLOCK_DISTANCE_ATR`)؛ قاعدة "اختراق عبر المنطقة بشمعة Displacement يحيّد الـ IFVG"؛ طابع تحذيري افتراضي (`IFVG_ENABLED`)؛ اختبارات واقعية على صعود/هبوط/متقلّب. |
| R8 | G7 داخل الإدارة: تحويل Retest مشروع إلى Exit مفرط | متوسط | مبدأ صارم: IFVG لا يغيّر القرار إلا بوجود دليل بنيوي/زخمي (MSS/CHoCH/BOS عكسي + ضعف momentum/liquidity)؛ والترند المحترم + IFVG مجرد Retest → **HOLD** وليس Exit (يُثبَّت باختبار). |

---

## 9) خطة الاختبار المقترحة (تُنفَّذ بعد الموافقة)

تقسيم موزّع:
1. **وحدة:** تفعيل `runner_bias`؛ TP من ATR لكل فئة أصل (حدود دنيا/قصوى).
2. **تكامل:** G2 Signals → تغييرات الـ health/الهيكل؛ G4 News levels → سلوك الاستحقاق.
3. **State Machine:** جدول الانتقالات لكل `TRADE_TYPE/MARKET_REGIME`.
4. **دورة حياة (PAPER حقيقي):** entry→TP1 جزئي→runner→حماية→breakeven→trail→خروج انعكاس/قامة.
5. **بيانات حقيقية:** 3 رموز × اتجاه صاعد/هابط/متقلب — قراءة OHLCV فعلي عبر `MarketSim` المعتمد (بلا API spam؛ cached).
6. **PAPER Lifecycle:** إثبات السجلات `[POSITION]` + `[TRAIL]` + `[PROFIT_LOCK]` + `[HARD_EXIT]` في الدورة.
7. **إجهاد 6 مراكز:** BTC/ETH + US500/USTECH + XAU + WTI + **الفتحة الخبرية مستقلة** (opt-in `NEWS_SLOT_ENABLED=True`) — استقلال كامل: SL/TP/trail/state/دورة لكل مركز؛ الهامش يطابق `balance + committed == 10000 + total_pnl`.
8. **انحدار:** الدفعة الكاملة؛ أجنحة المحفظة خضراء معًا؛ مقارنة الحالة قبل/بعد.

### أجنحة اختبار G7 (IFVG) المقترحة
- **وحدة المحرّك:** انتقالات الحالة لكل سيناريو مصنوع: NORMAL→MITIGATED→INVALIDATED→INVERSE؛ حساب `mitigation_ratio`؛ تجديد `retest.NONE/PROBING/SWEEP/REJECTED/BROKEN`.
- **تكامل الدخول:** OB ممتاز + IFVG عكسية قوية أمام الدخول → خصم درجة (`watch_score`) و`final_zone_score` ورفض/خصم عبر `check_institutional_entry`؛ وDisplacement عبر منطقة IFVG → تحييد وسماح.
- **تكامل الإدارة:** مركز مفتوح يواجه Retest لـ IFVG: بدون كسر بنيوي + زخم سليم → HOLD؛ مع MSS/CHoCH/BOS عكسي + ضعف زخم/سيولة → رفع Reversal-probability وإعادة التصنيف دون إغلاق فوري (يُترك للقواعد القابلة للتنفيذ).
- **تمييز الرفض من الاختراق المؤقت:** وسوالب/شمعة توسع داخل IFVG ثم التحرّر (sweep + reclaim) = رفض حقيقي؛ إغلاق فوق المنطقة دون متابعة = فخ ≠ اختراق حقيقي.
- **Six-position:** رمز واحد على الأقل بمسار دخول معرَّض لـ IFVG ضمن سيناريو الـ6 مراكز للتأكد من عدم تسريب التحذير لمركز آخر وأنه يُدار بشكل مستقل لكل مركز.

**سيناريوهات الـ18 المطلوبة (مرشح لختم القبول):** ترند حي، انعكاس مؤكد، انعكاس متوسط، Pullback حي، Pullback ضعيف، اختراق ناجح، اختراق فاشل، exhaustion، range، RSI المتطرفة، VWAP بعيد، OB/Zone بعيد، تمدد فولاتيليتي، تعادل، حالة news CRITICAL، حالة news HIGH، runner (ADX>25 صاعد)، حماية 5% يومي.
  _سيناريوهات IFVG تُدمج داخل الـ18 (خاصة: ترند حي مع Retest IFVG = HOLD؛ انعكاس مؤكد بوجود IFVG+MSS = Exited؛ اختراق ناجح عبر IFVG = دخول بعد التحايد)._

---

## 10) خريطة معايير القبول الـ14 (الوضع الراهن/الأدلة)

| # | المعيار | الحالة الحالية / خطة الإثبات |
|---|---|---|
| 1 | تصنيف ديناميكي كامل دائم | موجود + `position_trade_type/regime` في state. سُيثبت في تقارير سناريو. |
| 2 | الإدارة لا تُبني مكررًا | هذا التقرير يثبت REUSE (§3). |
| 3 | كل قرار مسجَّل `[POSITION]` كامل | موجود؛ سنُكمل حقول G2/G4. سجلات G7 سببها القرار: `[IFVG]` حالة الفجوة/الخصم/الوضع. |
| 4 | health score منفذ بحق | وجوده + قياس القرار من تعليقاته. |
| 5 | العزل التام لـ6 مراكز | `test_portfolio_dynamic_6way` يعبر الآن. |
| 6 | بوابات الدخول الفعلية موجودة ومحفوظة | ADX+sweep في `execute_entry`؛ نتحقق بعدم تجاوزها. G7 يمكن أن يخصم/يجهض عبر `check_institutional_entry` فقط — لا بوابة جديدة في `execute_entry` نفسها كي لا تتغير سلطة الدخول. |
| 7 | TP/SL في واجهة البورصة لا التطبيق | سيتحقق من كود close (لا native orders) ووثائق البورصة. |
| 8 | عدم إحصاء فتحة الأخبار في قبعة الفئة | كود `news_slot.py` + اختبار جديد. |
| 9 | سجل زمني (news stamp + بيانات مراقبة) تخص centrum المركز | يجب إنشاؤه (هذه أبرز فجوة إثبات). |
| 10 | لا إغلاق أعمى للأخبار | كود حالي + اختبار يحفظ "الخبر لا يُغلق بدون دليل". |
| 11 | سلوك الأصول مستقيم (لا مسميات مزيفة) | لا رموز مخترعة؛ `instrument not exposed` موجود. |
| 12 | اختبارات 6 المراكز الإجهادية | اختبار موجود + إضافة الفتحة الخبرية. |
| 13 | كل التدفقات تعبر بدون ظروف اختبار مزيفة | وضع الاختبار يطابق مسار PAPER الحي؛ لا stub على بوابات الحماية. |
| 14 | لا تحميل API زائد | رصد أن gauge/meter تستخدم cached + معدلات. |

---

## 11) IFVG Detection Engine — التصميم المؤسسي (G7)

### 11.1 التعريف (بصيغة تاجر محترف)
IFVG (Inverse Fair Value Gap) ليس مؤشرًا مستقلًا لإضافة تعقيد؛ هو **دورة حياة** لأي FVG قائم:
عندما يُملأ (mitigate) ثم يُبطل (invalidate) فجوة كان يُعول عليها كدعم/مقاومة، تتفلبل قطبية المنطقة وتصبح
بمثابة **منطقة مغناطيسية/عكسية** (الدعم السابق يتحول إلى مقاومة معاكسة أو العكس). النتيجة عملية:
لا نثق بفجوة "نوّر" اتجه السعر نحوها بعد أن فقدت صلاحيتها الأصلية.

### 11.2 مصفوفة الحالة لكل FVG
| الحالة | الشرط السعري (بأدلة شمعة/هيكل) | السلوك في البوت |
|---|---|---|
| **NORMAL** | السعر لم يعد داخل نطاق الفجوة بعد | إشارة موجبة خفيفة إن وُجدت في اتجاه الصفقة (كما `detect_fvg` الحالي) |
| **MITIGATED** | شمعة لاحقة اخترقت الفجوة جزئيًا (نسبة ملء `mitigation_ratio 0..1`) | تُخفف قوتها؛ مراقبة مصيدة (sweep): إن ارتدّ السعر بوسوالب عبر المنطقة وعاد (reclaim) تبقى المنطقة صالحة للاتجاه |
| **INVALIDATED** | السعر أغلق **عبر** الحد البعيد للفجوة (املاء كامل 1.0) | القطبية الأصلية غير موثوقة؛ المنطقة متاحة للانقلاب للقطبية المضادة |
| **INVERSE** | بعد INVALIDATED أو ملء عميق، المنطقة تُختبر **من الجهة المقابلة** وتنتج **رفضًا معاكسًا** (شرارة عكسية: ردة فعل من منطقة كان من المفترض أن تخدم الاتجاه الأصلي) | **تحذير:** تُطبق خصومات الدخول/الثقة وتُتابع Re-test وتُدخل في قوة الـ Reversal |

شروط فنية دقيقة (لتجنب الإنذارات الكاذبة):
- حجم الفجوة ≥ `IFVG_MIN_ATR` (افتراضي 0.5×ATR) — الفجوات الضئيلة لا تُعتبر IFVG.
- عمر الفجوة ≤ `IFVG_LOOKBACK` شمعة (افتراضي 60) — الفجوات القديمة جدًا خارج النطاق الاهتمامي.
- الانقلاب لا يُسجَّل إلا **بشمعة/ماسح بنيوي**، لا بمسّ واحد.

### 11.3 منطق الـ Retest (رفض حقيقي مقابل اختراق مؤقت)
| علامة في منطقة IFVG | التفسير | قرار البوت |
|---|---|---|
| وسوالب + إغلاق (reclaim Bar) بعيدًا عن المنطقة، بلا إغلاق بما وراء حدودها | رفض حقيقي | تشديد: الدخول معاكس مرفوض/مخصوم؛ لدى مركز القرار يميل للـ Reversal إذا اجتمعت علامات أخرى |
| إغلاق فوق الحد البعيد (لكسر) بلا متابعة/إغلاق عكسي سريع | اختراق مؤقت (فخ) | لا يُعتبر اختراقًا حقيقيًا؛ الحذر مستمر |
| إغلاق فوقها + Displacement متابعة + MSS/CHoCH | اختراق حقيقي | تتحيّد IFVG في اتجاه الصفقة: يُسمح بالدخول/يبقى التصنيف الطبيعي (TREND/PULLBACK) للصفقة المفتوحة |

### 11.4 سلوك البوت عند ظهور IFVG (كما طلب المستخدم)
1. **تحذير من الدخول** داخل المنطقة أو قبلها مباشرة (ضمن `IFVG_BLOCK_DISTANCE_ATR`، افتراضي 1.5×ATR).
2. **تخفيض ثقة الـ OB/Zone المتعارض:** في `get_smart_zones` يتم ضرب `strength` في `(1 − penalty)` وتسجيل السبب — يبقى `get_smart_zones` المزوّد المتعارف عليه.
3. **مراقبة الـ Retest** للـ IFVG وتحديث `retest.state` كل دورة (بتلف ذاكرته في `MEMORY["ifvg"][symbol]` لمنع إعادة الحساب الثقيل إلا مع شمعة جديدة).
4. **تمرير الدخول/القائمة المؤسسية:** خصم من `watch_score` (deep_scanner) ووزن `ifvg_alignment` داخل `final_zone_score` (قائمة الانتظار) وفحص في `check_institutional_entry` — **مع** قاعدة التحييد عند Displacement عبر المنطقة. والنتيجة الواضحة: "OB ممتاز وحدها لا تكفي إذا كانت IFVG عكسية قوية أمام الدخول".
5. **في مركز مفتوح:** لا يُعتبر تحركٌ نحو IFVG "Pullback عاديًا" تلقائيًا:
   - IFVG + MSS/CHoCH/BOS **عكسي** + ضعف Momentum/Liquidity → رفع Reversal-probability (يغذي `_run_advisory_health`/`_apply_dynamic_profit_and_exit_rules`) → عند تحقق إشارات قابلة للتنفيذ يُعاد التصنيف/الخروج (المسار الحالي للـ REVERSAL).
   - السعر يحترم الترند وIFVG مجرد **Retest سطحي بلا كسر بنيوي** → **HOLD وليس Exit** (يمنع الـ churn ويحفظ الرانر).

### 11.5 الواجهات المقترحة (تُدوَّن في التقرير ولا تُنفَّذ الآن)
```python
# core/engine.py (بجوار detect_fvg:6324)
def detect_fvg_lifecycle(df, lookback=60, threshold=0.001, min_atr=None):
    # -> list[dict]: {side, top, bottom, age_bars, state, mitigation_ratio,
    #                 retest:{state,last_reaction}, inverted_bar}

def ifvg_warning_payload(symbol, side, df, atr, entry_price=None):
    # -> {has_inverse, blocking, distance_atr, penalty, reasons[]}
```
===
ملاحظة: الواجهات أعلاه **وصف تصميمي** فقط — لم تُكتب في أي ملف إنتاجي.

### 11.6 الحواجز التصميمية (تجنب الإفراط/التعقيد)
- `IFVG_ENABLED=True` Opt-in؛ والافتراضي في الاختبارات ON.
- `IFVG_PENALTY_MAX=0.30` — IFVG وحده لا يُجهض فرصة نهائيًا، يحوّل الاحتمالات/الثقة فقط.
- الفصل Advisory/Enforceable مستمر: IFVG في المدير → يستحق/يتصنّف؛ والخروج الفعلي يبقى من القواعد القابلة للتنفيذ (Phase-2) بعد التحقق.
- لا بوابات جديدة في `execute_entry`؛ G7 يعمل عبر الوجهات المعتمدة (القائمة المؤسسية/خصم @ الدخول/ثقة المناطق) — فتبقى سلطة الدخول والخروج كما هي مُجرَّبة.
- الأداء: حِساب المستوى = O(bars) على `df` المخزّن (`get_ohlcv_safe`) بلا أي استدعاء API إضافي؛ والتخزين المؤقت `MEMORY["ifvg"]` يمنع الإعادة إلا عند شمعة جديدة.

---

## 12) التوقف قبل التنفيذ

**لم يُعدَّل أي ملف إنتاجي.** المراجعة الشاملة اكتملت، والفجوات محددة بدقة (G1–G7)، والاختبارات الجديدة الحالية تعبر (`test_portfolio_dynamic_6way` + أجنحة المحفظة).

بانتظار الموافقة على أحد الخيارات:
- **(أ)** الموافقة على الخطة كما هي → نبدأ التنفيذ (بادئًا باختبارات G1/G2/G3/G4/G7 ثم التنفيذ ثم الدورة الكاملة ثم الإجهاد ثم الانحدار).
- **(ب)** تعديل نطاق الفجوات (مثلاً: G4/G5 مؤجلتان لمرحلة لاحقة).
- **(ج)** وقف المرحلة.