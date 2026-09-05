# Forensic + Architecture Review — Causal OB as Single Source of Truth (ATOM Execution Architecture)

> المرحلة: **مراجعة فقط — لا تعديل كود إنتاج**. جنائية (OB) + معمارية (مسار التنفيذ إلى OPEN).
> كل إسناد برمجي Below = `core/engine.py` ما لم يُنص غير ذلك. المسار الرئيسي المُدار = ExecutionQueue/RF Liquidity Engine v28.

---

## 0. Executive Summary

- **النظام الحالي يحوي كاشفَي أوردر بلوك متوازيين وغير متوافقَين**: مسار الـ Grading (A+/A/B) يعتمد على `detect_order_block` / `compute_order_block_quality` البدائيَّين، بينما المنطقة المعتمدة للدخول تأتي من `_find_causal_ob_zone` الصارم. الـ Grade قد يُبنى على شمعة مختلفة عن شمعة الـ Zone المتداولة (ثغرة قاتلة لمسار A-Grade سريع-التأكيد).
- **`compute_order_block_quality` يحوي خطأ قياس**: انزياح السعر يُقاس من "آخر شمعتين من الـ DataFrame" وليس من شمعة الـ OB إلى ساق الاندفاع التالية، مع حارس ميت `if idx >= 0` لا يُسلك أبدًا.
- **الأبعاد العلمية الأربعة (Sweep / FVG / Premium-Discount / BOS-causal) غائبة عن تقييم جودة الـ OB ذاته**؛ السويت يُقرأ مؤقتًا فقط، ومسار roro يجعله حاجزًا صلبًا بينما مسار الـ Queue يجعله تعزيزًا (تناقض فلسفي).
- **آلية التأكيد (Confirmation) معطوبة في المسار العادي**: التوكيد الثاني ينتظر "توقيعًا مختلفًا"؛ في سوق مجمّد يبقى التوقيع ثابتًا فلا يتراكم العدد أبدًا، ويُستعاض عنه بـ fallback في `get_best_candidate`.
- **البنى المطلوبة اجتماعيًا موجودة جزئيًا**: فتحات 2/2/1/1 في `GlobalAssetAllocator``CLASS_CAPS`، وفتحة NEWS منفصلة (opt-in) في `core/runtime.py:331`، وتصنيف ديناميكي في `DynamicPositionProfile` لكن **بدون NEWS** و**استشاري فقط** (Phase 1)، وتكيّف لكل أصل في `AssetBehaviorProfile` لكن **غير قابل للتطبيق الفعلي** بعد.
- **الخلاصة**: لا نحتاج "عينات جديدة تعرقل الدخول"؛ نحتاج توحيد مصدر الحقيقة + إصلاح القياس + إضافة الأبعاد الأربعة كتعزيزات مرجّحة + إصلاح التأكيد ليكون زمنيًا/سوقيًا + جعل إدارة المركز قابلة للتنفيذ وديناميكية.

---

## 1. المعمارية الحالية (As-Is) — خريطة الكود الفعلية

```
GLOBAL SCANNER  -> scanner/scanner.py + scanner/deep_scanner.py  (yor utf8 + orderbook + watchlist seed)
WATCHLIST       -> MEMORY["watchlist"] + ExecutionQueue._candidates  (states: DISCOVERED..WATCHLIST)
STRONG          -> GOOD_ZONE / WAITING_TRIGGER / ENTRY_VALIDATION + roro strong_ob (A+/A)
WAITING LIST    -> get_best_candidate() (READY أولاً) ثم eligible-fallback (:12060-12083)
OB/Zone Validate-> _evaluate_order_block(:11219) _find_causal_ob_zone(:11300) _select_strong_ob(:11787)
                  _evaluate_liquidity/_evaluate_structure/_detect_institutional (:11078-11085)
                  _detect_trigger_state(:11639) + zone lifecycle (:11178)
ENTRY READY     -> _update_state(:11913) => READY  (مسارات الشرط في :11971-11974)
Safety Gates    -> can_open(manager:70) + risk_guard + kill switch + IFVG penalty + allocator caps
OPEN TRADE      -> portfolio/manager.open_candidate(:156) -> engine.execute_entry(:6176)
                   -> LiveTradeManager(:3258) + DynamicPositionProfile(:2264) + AssetBehaviorProfile(:2207)
NEWS SLOT       -> core/runtime.py:331 execute_news_slot (NEWS_SLOT_ENABLED opt-in) -> portfolio/news_slot.py
```

**تماسك Flo**: `ManagedQueue._re_evaluate` (نحو :11040-11162) هو حلقة التقييم الفعلية لكل رمز: يقيّم البيلرات الثمانية ثم `_detect_trigger_state` ثم يؤكد ثم `_update_state`.

---

## 2. المعمارية المستهدفة (To-Be) vs الحالي — جدول الفجوات

| المرحلة المستهدفة | الحالة الحالية | الفجوة |
|---|---|---|
| GLOBAL SCANNER | موجود (deep scanner + RADAR + universe) | تنسيق Universe/queue/OB واحد |
| WATCHLIST | موجود (`ExecutionQueue`) | — |
| STRONG | موجود (GoodZone/Trigger) | يعتمد جزئيًا على الـ naive OB |
| WAITING LIST | `get_best_candidate` + allocator | ترتيب `priority_score` أعمى عن جودة OB/Zone الحقيقيّة (الشبكة الأربعة كتعرية) |
| Institutional OB/Zone validation | `_select_strong_ob` + `_find_causal_ob_zone` | **مصدران متعارضان للـ OB (F1)** |
| ENTRY READY | `_update_state` | تأكيد معطوب (F4) + score يعتمد على naive |
| Safety Gates | risk_guard + IFVG + caps | تسجيل الرفض غير موحّد (F9) |
| OPEN TRADE + إدارة ديناميكية | `execute_entry` + LiveTradeManager | الديناميكية استشارية، بلا NEWS |
| فتحات 2Crypto/2Index/1Gold/1Oil | `allocator.CLASS_CAPS` فقط | `manager.can_open` لا يفرض السقوف (env=999) |
| فتحة NEWS منفصلة | موجودة opt-in | ليس لديها `trade_type=NEWS` ولا تُختبر في sim موحّد |
| classification فوري بعد OPEN | `_ensure_position_profile` | TRADE_TYPES بلا NEWS؛ النوع يأتي من mapping قديم |

---

## 3. نتائج الفحص الجنائي للـ Order Block (Advice: مؤكدة بالكود)

### F1 — أربعة مسارات متوازية للـ OB، والـ Grading على المسار البدائي
| المسار | الموقع | طبيعته | مستخدَم في |
|---|---|---|---|
| `detect_order_block` | :6502 | بدائي: آخر إغلاق ≥1.2ATR، ثم أول شمعة معاكسة خلال 4 | input لـ F2 |
| `compute_order_block_quality` | :10071 | مستند للبدائي + قوة سببية (4 عائلات) | **ob_score لأجل Grading (A+/A/B)** |
| `_evaluate_order_block` | :11219 | صارم (Displacement 0.6ATR + اتجاه + touches + broken) | `ZoneMetrics.order_block_quality` (وزن .12) |
| `_find_causal_ob_zone` | :11300 | صارم، منطقة السبب الفعلية | **الـ zone المتداولة + disp + touches في Grading** |

> الأثر الخطير: `_select_strong_ob` (:11787) يعتمد `ob_score` من F2 على شمعة قد تكون مختلفة عن `zl/zh` من F4. وهو ما يغذّي `strong_ob_present` و`is_a_grade` والتأكيد الفردي. **توحيده إلزامي**.

### F2 — أخطاء `compute_order_block_quality`
- `if idx >= 0: return 30,"NONE"` (:10083-10084): **حارس ميت** — `detect_order_block` يرجع دومًا idx سالبًا؛ فرع NONE لا يُسلك أبدًا، ومعنى "لا وجود لـ OB" لم يعد يمثل.
- `origin = df.iloc[idx]` ثم `future_high = df['high'].iloc[-2:]` (:10087-10095): **يزوح السعر إلى آخر شمعتين من الـ DataFrame** وليس إلى ساق الاندفاع التالية مباشرة بعد شمعة OB؛ لو كان OB قديمًا (idx=-4/-5) تُقاس المسافة خطأً، وتُطبق عائلات 40/60/80/90 على قياس ملوّث.
- النتيجة: score غير سليم يدخل درجات A+/A/B + `OrderBlockQuality` في `_classify_opportunity`.

### F3 — الأبعاد العلمية الأربعة مفقودة من "جودة الـ OB"
- **Liquidity Sweep**: في الـ Queue يُقرأ **وقت المحفّز** فقط (`_detect_trigger_state` :11646-11654) لا كصفة للـ OB. وفي roro (`check_institutional_entry` :6008-6011) هو **حاجز إلزامي**. تناقض بين الفلسفتين.
- **FVG/Imbalance**: موجود كبديل لنقطة لمس في roro فقط (:6029-6037) + IFVG للتتبّع التحذيري (:6531+). **ليس وزنًا في الـ OB score/Grading**.
- **Premium/Discount**: **غائب كليًا** من مسار الـ OB والـ Grade و`_evaluate_zone_strength`.
- **BOS/CHoCH سببي**: لا ربط بين بار الـ OB وBOS/CHoCH التالي؛ الهيكل يدخل عبر `structure_alignment` (وزن .15) والمحفّز فقط.
- **الحالة الصحيحة**: يجب أن تكون هذه أبعادًا **تعزيزية مرجّحة** (لا حاصرات) تحت سقف 100.

### F4 — آلية التأكيد: تسمّم انتظار توقيع متشابه
- الحلقة :11088-11096: `if sig != cand.last_confirm_signature: confirmation_count += 1`.
- التوقيع (:11722) يشمل trigger + sweep + structure_score + rejection + absorption + zone. في السوق المتجمد (نفس الشموع) لا يتغير → لا يتراكم العدد، ويعلق المرشح القوي في WAITING_TRIGGER، ويُستخدم fallback (`get_best_candidate` :12074-12083) لتمرير مرشح بمحفّز مؤكد دون READY.
- المطلوب: تأكيد مبني على **أدلة سوقية متتالية/زمنية** (شمعة جديدة، محفّز جديد، تحرك ATR، تحرك سعر) — لا زيادة كل دورة، ولا اعتماد على تعرّج التوقيع.

### F5 — توزيع الأوزان
- `final_zone_score` (:10450-10466): `order_block_quality` وزن **0.12**، `zone_strength` **0.18**، `liquidity_quality` **0.18**. الـ OB يعمل أكثر كبوّابة (≥40 في العائلة "zone") منه كعامل تمييز مستمر — توحيده وتحسينه سيؤثر إيجابًا على التدرّج بدل "نعم/لا".

### F6 — إدارة المركز: إستشارية وبلا NEWS
- `DynamicPositionProfile` (:2264): `TRADE_TYPES = (TREND/REVERSAL/BREAKOUT/PULLBACK/RETEST)` — **بلا NEWS**؛ والـ mapping يرمي أي تصنيف دخول لم يطابقه إلى TREND.
- `PositionManagementEngine.compute` (:2375): **استشاري فقط** (ACTION advisory + PHASE 1 comment :2198-2200) — لا ينفّذ TP1/TP2/runner/trailing ديناميكيًا بعد.
- `AssetBehaviorProfile` (:2207): معاملات لكل أصل (CRYPTO/INDEX/GOLD/OIL/NEWS) **موجودة** — البنية التكيّفية جاهزة لكن غير مفعّلة سلوكيًا (sl_mult يُطبَّق في `set_entry_atr` فقط :3307-3318).
- "لا trend=False دائم": لا وجود لرمز دائم `trend=False`؛ لكن **الفجوة الحقيقية** أن classification واحد عند الفتح لا يُعاد تقييمه سلوكيًا — وهذا ما يجب إكماله، لا "قلب boolean".

### F7 — الفتحات الصنفية
- `allocator.CLASS_CAPS = {CRYPTO:2, INDEX:2, GOLD:1, OIL:1, NEWS:1, STOCK:2, ...}` موجود (:32-41). فتحات 2/2/1/1 ممثلة في المخصص.
- **لكن**: `manager.can_open` (:70-79) يستخدم `MAX_POSITIONS_PER_ASSET_CLASS` الافتراضي **999** — أي أن البوابة عبر `open_candidate` لا تفرض السقف؛ التفرض الوحيد من المخصص، و`open_top` (اختبارات 6-way) يتجاوز المخصص. يجب توحيد: نفس نموذج السقف في `can_open` نفسه حتى كل مسار فتح يلتزم 2/2/1/1 WITHOUT أن يمنع "الأقوى".
- `SIDE_CAPS BUY/SELL=4` موجود؛ لا افتراضية مضادة لتفريغ الفتحات.

### F8 — فتحة NEWS
- موجودة opt-in (`NEWS_SLOT_ENABLED`, `core/runtime.py:331`)، سقف مستقل `count_open_news(PORTFOLIO) >= 1` (:371)، تمرّ عبر نفس مسار الأمان `open_candidate->execute_entry` (:382-390).
- الفجوات: لا `trade_type=NEWS` في الملف الديناميكي، ولا تحويل صريح للـ `asset_class=NEWS` عند الفتح إن لم يُمرَّر، ولا اختبار sim موحّد يثبت مسارها إلى رحلة إدارة كاملة.

### F9 — تسجيل الرفض
- `record_gate_event` يغطي بوابات الـ Queue (ZONE_STALE/PRICE_EXTENDED/ORDER_BLOCK_BROKEN/SCORE_TOO_LOW/READY...) مع reason نصّي؛ و`runtime` يضبط outcomes (`news_*`, `_exec_gate`).
- **الفجوة**: عدة مسارات تعيد `False` بلا بوابة مسجّلة: `can_open` (bool بلا سبب موحّد)، `_check_entry_conditions`، `council_exit`، رفضات `check_institutional_entry` (reason نصي فقط لا gate). مطلوب بنية رفض واحدة: `(terminal_gate, reason, context)`.

---

## 4. تحليل الفجوات مقابل متطلباتك (A–D + إدارة)

| المطلب | الحالة | الفجوة المطلوبة للتنفيذ |
|---|---|---|
| OB واحد مصدر الحقيقة | ✗ | توحيد المسارات الأربعة على `_find_causal_ob_zone`؛ الـ Grading يقيّم نفس الـ OB |
| إصلاح قياس displacement | ✗ | من شمعة OB نفسها إلى ساق الاندفاع التالية؛ إزالة الحارس الميت |
| إضافة Sweep/FVG/P-D/BOS-causal كتعزيزات | ✗ | وزن مرجّح (لا حاصرات) يدخل score OB + Grade + `confluence_bonus` |
| إصلاح التأكيد | ✗ | عدّ زمني/سوقي (شموع جديدة/محفّز/ATR)، لا تجميد ولا زيادة كل دورة |
| لا تمييع للـ READY | ✓ (لا تلامس العتبات) | أولًا تحقق إمبريقي من توزيع scores بعد الإصلاح ثم فقط أي ضبط |
| WAITING LIST يقارن المرشحين | جزئي | ترتيب يعتمد `final_zone_score` المعاد (OB موحّد) + العائلات المؤسسية |
| فتحات 2/2/1/1 | جزئي | فرض السقف في `can_open` نفسه (كل مسار فتح) |
| فتحة NEWS مستقلة | جزئي | `trade_type=NEWS` + تحويل asset_class + sim موحّد |
| تصنيف فوري بعد OPEN TREND/PULLBACK/BREAKOUT/REVERSAL/NEWS | جزئي | إضافة NEWS للـ TRADE_TYPES وتفعيل `update()` ديناميكيًا عند الفتح |
| إدارة أرباح احترافية (TP1/TP2/runner/trail/تشدّيد/احتجاز) | ✗ | تحويل PositionManagementEngine من استشاري إلى قابل للتنفيذ وفق trade_type + asset_class |
| تكيّف ATR/volatility/structure/liquidity/flow | جزئي | AssetBehaviorProfile موجود؛ تفعيله سلوكيًا (TP/trail لكل أصل) |

---

## 5. خطة تحقيق Emprical قبل أي تعديل عتبات

1. تشغيل `_evaluate_order_block` المحدّث (قبل/بعد البونصات) على دفعات OHLCV حقيقية/تركيبية لعيّنة ≥ 50 رمزًا وتسجيل Histogram لـ `ob_score` و`final_zone_score`.
2. قياس نسبة المرشحين الواصلين لـ READY **قبل** أي تعديل عتبات (baseline).
3. فقط إن أظهر التوزيع انزلاقًا بنيويًا (كلاسيكيًا نحو الأسفل) — لا نُعدّل عتبة، بل نصحّح الوزن/القياس ثم نعيد القياس.

## 6. مخزون الاختبارات الحالي مقابل المطلوب

| مطلوب | موجود جزئيًا | الأدلّة |
|---|---|---|
| A) Static/regression | ✓ | +200 اختبار: `test_institutional_queue`, `test_regressions`, `test_profit_engine_phase3`, `test_position_management_phase1`, `test_orderbook_side_identification` ... |
| B) Synthetic forensic | جزئي | OB displacement/FAKE/FRESH (queue)، سيناريو 6-way (portfolio)، IFVG spec (phase3) — **يفتقد: false-positive sweep / FVG / broken-OB-as-大清reinforcement / news-event scenarios كوحدة موحّدة** |
| C) Live-market connectivity | جزئي | `real_pipeline` (test_runtime_repairs) يعمل بالخطأ البيئي — يحتاج real OHLCV/orderbook/watchlist مسيطر عليه |
| D) Full execution sim | جزئي | `test_portfolio_dynamic_6way` + `test_profit_engine_phase3`: 6 مراكز OPEN + manage cycle — يحتاج: ≥6 qualified → WAITING LIST → 6 OPEN → lifecycle TP1/TP2/trail + مسار NEWS منفصل 끼 sim |

**الملاحظة**: لا نستخدم "fake dashboard script" كدليل؛ الاختبار D يجب أن يفرغ `open_candidate->execute_entry->manage_all/activate` الحقيقي مع كتم الحدود الخارجية (order/settlement) فقط.

---

## 7. ترتيب التنفيذ المقترح (بعد اعتماد هذه المراجعة — لا كود الآن)

1. **T1 توحيد الـ OB**: `compute_order_block_quality` يعيد تقييم الـ causal zone نفسها (مرر idx)، إزالة الباث البدائي من مسار Grading؛ `_ob_synergy` مساعد واحد للبونصات الأربعة في المسارين.
2. **T2 منظومة تعزيز مدمجة**: sweep/FVG/P-D/BOS-causal كوزن مرجّح في `_evaluate_order_block` + `_select_strong_ob` + `confluence_bonus` في `final_zone_score` (افتراضي 0 لعدم كسر أي شيء).
3. **T3 إصلاح التأكيد**: عدّ على أساس الحدث (شمعة جديدة/محفّز جديد/سعر/ATR تجاوز)، لا تجميد التوقيع، ولا زيادة تلقائية.
4. **T4 فتحات**: فرض CRYPTO2/INDEX2/GOLD1/OIL1 في `can_open` نفسه + NEWS cap مستقل؛ إبقاء البوابات الأخرى دون تشديد.
5. **T5 إدارة ديناميكية**: إضافة NEWS للـ TRADE_TYPES؛ تفعيل `update()` عند OPEN؛ تحويل PositionManagementEngine من استشاري إلى قابل للتنفيذ (TP1/TP2/runner/trail مبنية على `AssetBehaviorProfile`) مع **عدم إغلاق ترند قوي عند تراجع مؤقت** واعتبار pullback سليم والحفاظ على الهيكل.
6. **T6 فتحة NEWS موحّدة**: تحديد دوري معروف + تحويل asset_class إلى NEWS + trade_type NEWS.
7. **T7 اختبارات**: A static + B synthetic forensics (كل العشرة سيناريوهات) + C connectivity حقيقي (خارجي مسيطر) + D full-execution sim (6 مؤهل → Waiting List → 6 OPEN → lifecycle + NEWS path) — مع تسجيل كل رفض `(terminal_gate, reason)`.
8. تشغيل كامل الـ suite + الـ sim؛ لا اندماج قبل أن يمرّ كلاهما.

---

## 8. بنود الالتزام (نوجدها في التقرير النهائي عند الاندماج)

- لا حذف لأي منطق؛ كل تغيير "إضافي/تعزيزي".
- لا تغيير أسماء `ExecutionState` ولا بنية `ExecutionCandidate`.
- لا خفض عتبات لإرضاء الاختبارات.
- الـ OB السببي هو مصدر الحقيقة الوحيد في: Grading / Zone score / A+/A / liquidity & FVG & premium-discount / displacement / freshness / entry.
- كل رفض → (terminal gate, reason) في السجل.
- التأكيد = دليل سوقي متتالي، لا تجميد، لا إجبار.