# مراجعة هندسية لنظام إدارة الصفقات (Deals Management) — تقرير الأخطاء

التاريخ: 2026-09-07
النطاق: `portfolio/manager.py`, `core/engine.py`, `execution/execution_service.py`, `dashboard/app.py`, `app/bootstrap.py`, `core/runtime.py`
طريقة التحقق: قراءة الكود + تشغيل الاختبارات (44 + 34 + 10 passed) + أداة `tools/six_position_runtime_validation.py` (PASS).

> ملاحظة منهجية مهمة: كل الاختبارات الحالية والأداة التسلسلية تنجح لأنها تعمل بخيط واحد وبترتيب تسلسلي متعمد. الأخطاء المسرّدة أدناه لا تظهر في الاختبارات؛ هي أخطاء معمارية تظهر عند التشغيل متعدد الخيوط (الوضع الإنتاجي الفعلي) وعند مقارنة المنطق بين المسارات.

---

## 1) خلاصة تنفيذية

نظام إدارة الصفقات مبني على "نواة موثوقة" مفردة (`LiveTradeManager`) هي السلطة الوحيدة على SL/TP/الإغلاق الجزئي/المطاردة (trailing)، مع طبقة `PortfolioManager` تعزل حزام الرموز عبر نسخ عميق من `STATE` العمومي وتشغيل رمز واحد في كل دورة. الفكرة سليمة معمارياً، لكن التنفيذ يعتمد على **حالة عمومية واحدة قابلة للتغيير (`STATE`, `TRADE_STATE`, `_live_manager`, `_closing_in_progress`, `event_bus`)** تجعل أي خيط ثانٍ (لوحة التحكم/الويب/مزامنة البورصة) قادراً على إفساد دورة إدارة رمزٍ آخر. أبرز الأخطاء بالترتيب:

1. **E-01 سباق خيوط حاسم على الحالة العمومية** — أعنف خطأ وأكثرها تأثيراً.
2. **E-02 تلوث متبادل بين الرموز عبر الحافلة العمومية** `event_bus`.
3. **E-03 خلل تتابع الإغلاق: بوابة إغلاق ثانية متداخلة** (`council_exit` + `manage_live_trade`).
4. **E-04 محاسبة PAPER غير متسقة الأساس المرجعي** (Notional مقابل ROE).
5. **E-05 `sync_position_state` يمسح صفقة أُغلقت خارجياً دون تحصيل PnL** (فقدان أرباح في LIVE).
6. **E-06 عزل الحالة هشّ وغير قابل للانفكاك عند الاستثناءات؛ والقراءة المباشرة للداشبورد من `STATE`**.
7. **E-07 `ExecutionService.close(symbol)` يتجاهل الرمز تماماً**.
8. **E-08 إحصاءات مضللة**: عدد الصفقات لكل رمز يُزداد لكل ساق جزئية، والنسب على أساس مختلط.
9. **E-09 حرجة**: كتلة TP1 في الإدارة بلا بوابة سعر — إقفال النصف الأول يحدث على `tp1_hold_score < 8` وليس عند وصول السعر للهدف.
10. **E-10 أثر مباشر لـ E-09**: إغلاق TP2 محجوب خلف `tp1_hit` فيتعطل "السلم" كاملاً والأهداف المعروضة تجميلية؛ الدليل: صفقتان حيتان بـ ROE +49.95%/+29.24% والسعر متجاوز TP1/TP2 والمدارة تبقى "ENTRY → MONITOR" بلا تحصيل.

---

## 2) بنية النواة الموثوقة (كما هي سليمة)

- مصدر الارتقاء: `execute_entry` (engine.py:8237) — إعادة اشتقاق SL/TP بعد التعبئة عبر `_reconcile_levels_after_fill(..., force_validate=True)` (engine.py:8605) وحارس هندسة الاتجاه `_enforce_sl_tp_geometry`.
- الإدارة: `PortfolioManager.manage_all` (manager.py:261) يستحضر كل رمز بالتبادل عبر `activate`/`deactivate` (manager.py:136/164) مع `_capture`/`_blank` (manager.py:110/123)، ثم `_live_manager.manage_live_trade()` (engine.py:4366) تحت `_TRADE_LOCK`، وتتحقق من وجود صفقة خارجياً عبر `sync_position_state`.
- دفتر التدقيق: `core/trade_journal.py` + `_journal_trade_event` لأحداث `TRADE_OPENED..TRADE_CLOSED`.
- الأخطاء التالية لا تنقض هذه البنية، بل تلغي ضماناتها الصريحة (العزل، ومصدر واحد للإغلاق، وعدالة المحاسبة).

---

## 3) الأخطاء الحاسمة (حرجة)

### E-01 — سباق خيوط حاسم على الحالة العمومية للأولويات (Critical)

**الوصف**: كل منطق الصفقة يعمل على `STATE`/`TRADE_STATE`/`_live_manager` معرّفة على مستوى العمود `core.engine` (module-level globals). بنية التشغيل في `app/bootstrap.py:54-62` تشغّل:
- `supervisor_thread` → `R.safe_main_loop` (دورة `manage_all` + تنفيذ قائمة الانتظار) في خيط خلفي (daemon).
- لوحة التحكم `DASHBOARD_APP.run()` (bootstrap.py:78) في **الخيط الرئيسي**؛ خادم Flask يفتح خيطاً جديداً لكل طلب.

لواحق الويب تفاعل مع نفس الحالة العمومية لأنها تستدعي `PORTFOLIO` مباشرة:
- `/trade` (dashboard/app.py:1388) → `PORTFOLIO.open_candidate` → `activate(symbol)` (manager.py:189) الذي **يمسح `engine.STATE` العمومي ويملؤه بحالة الرمز** ثم يشغّل `execute_entry` ثم `deactivate`.
- `/close` (dashboard/app.py:1421) → `PORTFOLIO.close_symbol` → `close_position_full()` على `STATE` العمومي.

**التتابع الفعلي**: أثناء وجود خيط الإشراف (supervisor) داخل `manage_live_trade` لرمز A (بداخل `_TRADE_LOCK`)، يمكن لخيط Flask أن:
1. يمسح `STATE` ويملؤه بحالة الرمز B (`activate`, manager.py:155-159) → نصيب رموز يلتصق في نافذة إدارة رمز آخر.
2. يحدث `_closing_in_progress`/`_reconciliation_pending` (engine.py:3602-3603) فيرفض `close_position_full`/`close_partial` المشروعة في دورة الرمز نفسه بـ "Already closing, skipping" (engine.py:3597).
3. يغلق فعلياً رمزاً نشطاً في `STATE` لا يستهدفه المستخدم (لأن `close_position_full` يغلق "الموجود الحالي" وليس الرمز المطلوب).

**فضل الرابط في ما يضمنه إطار العمل**: `_TRADE_LOCK` يحمي جسم `manage_live_trade` فقط (engine.py:4383)، أما `close_position_full` و`close_partial` فلا يوضعان تحت أي قفل على مستوى النواة. قفل `PortfolioManager._lock` (manager.py:36) RLock واحد يُسلسل فقط عمليات الـ manager نفسها؛ ولا يحمي من تداخل خيط Flask عبر مسارات النواة.

**المخاطر**: إغلاق رمز خاطئ، فقدان/خلط حالة صفقة مفتوحة، رفض إغلاقات مشروعة، قراءة dashboard لموضع من خيط آخر. لا يمكن إثباته بالاختبارات الحالية لأنها أحادية الخيط.

**الحل**: فصل الحالة عن النواة — كائن `PositionState` خاص بكل رمز لا يُنسخ من/إلى العمومي؛ إضافة `threading.RLock` موحَّد حول كامل مدار التداول (من `activate` حتى `deactivate`، ومن `close_*`)، أو اعتماد "مفاعل" واحد للتسلسل بدل الخيوط المتوازية في لوحي الويب والتحكم.

---

### E-02 — تلوث متبادل بين الرموز عبر الحافلة العمومية `event_bus` (Critical)

**الوصف**: `LiveTradeManager.__init__` يشترك في الحافلة العمومية نفسها لكل أحداث النواة (engine.py:3819-3821):
```python
event_bus.subscribe("reconciled", self._on_reconciled)
event_bus.subscribe("force_close_local", self._force_close)
event_bus.subscribe("lifecycle_change", self._set_lifecycle)
```
كل رمز مرتبط بمثيل `LiveTradeManager` خاص (يُنشأ في manager.py:148-153)، لكن الحافلة واحدة مشتركة بين كل المثيلات.

**أثر الخلل**:
- `_set_lifecycle` (engine.py:3823-3826) يكتب مباشرة `DASHBOARD_STATE["lifecycle_state"]` **بدون فحص رمز** — درجة الحياة المعروضة تعكس لاعب "آخر منبعث" وليس بالضرورة الرمز النشط.
- `_on_reconciled` (engine.py:3828-3832) يضبط `DASHBOARD_STATE["live_trade_mode"]=True` ويحدّث `current_snapshot` **لجميع المثيلات الستة** عند أي `reconciled` لأي رمز من خيط مزامنة البورصة؛ المثيلات غير النشطة تصبح "حيّة خاطئاً".
- مقابل ذلك: `_force_close` (engine.py:3838-3847) هو **الفلتر الوحيد المقيد بالرمز** — وهذا يثبت أن بقية المعالجات غير مرشّحة، أي أن التصميم لم يُعمِّم ربط الرمز على بقية الحافلة.

**الحل**: إرسال `symbol` في كل حدث، وفلترة داخل كل معالج بأن `symbol == STATE["current_symbol"]`.

---

### E-03 — خلل تتابع الإغلاق: بوابة إغلاق ثانية متداخلة (High)

**الوصف**: في نفس الدورة الواحدة يمر الرمز عبر **قناتي إغلاق مستقلتين**:
1. داخل `manage_live_trade` → `_apply_management` (engine.py:4417) الذي يملك SL/TP/الإغلاق الجزئي/المطاردة ويستدعي `close_partial`/`close_position_full` بحسب الحالة.
2. بعدها مباشرة في `manage_all` (manager.py:279):
```python
if self.engine.council_exit(df, price): ...
```
و`council_exit` (engine.py:10066-10083) يغلق على **شرطين لا يمثّلان منطق مدير الأرباح**:
- `ADX < 18` (engine.py:10069) — إغلاق ضجيجي (whipsaw) قد يفك موقفاً في فيه Momentum/Profit lock حمايةً نشطة، دون تنسيق مع `_apply_management`.
- كسر `synthetic_sl` (engine.py:10073/10078) — إعادة إنشاء منطق SL الذي يملكه أصلاً `LiveTradeManager`، في مكان ثانٍ.

**وجود متزامن**: خيطان الاسلوب متفرقان فيepistem حقائق خيمة إدارة واحدة. عند بطء مصدر بيانات (get_ohlcv_safe تفشل داخل `_apply_management` فتعود فوراً، manager.py:277 "if df is not None")، قد ينفّذ `council_exit` بوابة الإغلاق بينما `_apply_management` لم يكمل تقديرُ حالته — السلوك النهائي يعتمد على ترتيب التنفيذ لا على منطق موحّد.

**حالات هامشية موثقة**:
- في المسار القديم `main_loop` (engine.py:12006-12011) بعد `council_exit` يُستدعى `finalize_trade_with_reality(sym)` **بدون شرط فحص** — والدفاع اللاحق (manager.py:280-285) الذي أضيف هو بمعزل عن هذا المسار، أي أن المسار القديم ما زال عرضة لإزدواجية التحصيل.
- `council_exit` يغلقه `close_position_full()` التي تحصّل وتُنهي (engine.py:3611/3651/3660/3682) — أي حالة "أغلق العنوان مع فكره" موجودة ضمن النواة.

**الحل**: مصدر واحد للإغلاق النهائي (بالبوابة الموحدة داخل `_apply_management` + `close_position_full`)، وحذف/تعطيل `council_exit` من `manage_all` أو تحويله إلى "ترشيح" يسجّل اقتراحاً فقط؛ وإغلاق المسار القديم.

---

### E-04 — محاسبة PAPER غير متسقة الأساس المرجعي (High)

**الوصف**: النسب المحصلة تُحسب على أساسين غير متوافقين، وتغذي المؤشرات الداخليّة بقيم مختلفة:

| القناة | المعادلة | الأساس |
|---|---|---|
| `sync_position_state` PAPER (engine.py:4958-4959) | `raw_pnl * LEVERAGE` | ROE **مرفوع برافعة** |
| `_apply_management` / مدير الحماية يعتمد على `roe_pct` | نفسه أعلاه | مرفوع |
| `close_partial` PAPER (engine.py:3504) | `dirv*(mark-entry)/entry*100` | **اسمي غير مرفوع** |
| `finalize_trade_with_reality` PAPER (engine.py:7183-7186) | نفس الصيغة الاسمية | **اسمي** |
| `_credit_realized_pnl` → `PERF["total_pnl_pct"]` (engine.py:8136) | جمع أعلاه | مختلط |

**الأثر**:
- `PERF["total_pnl_pct"]` يجمع نسبة كل ساق جزئية بـ**الوزن الكامل** رغم أن الساق تعادل ثلث الكمية خمز: إغلاق 1/3 عند +2% يزيد `total_pnl_pct` بمقدار 2% رغم أن المحفظة حصدت ~0.67%؛ مع ثلاث ساق = إجمالي مغمور.
- "الربح/الخسارة" المعلن في dedication يعتمد على `roe_pct` المرفوع، بينما `PERF` الاسمي — الاثنان مختلفان وتُعرضان متجاورين في الواجهة دون تناسق.
- `realized_roe_pct` (engine.py:7226, 8158) = `realized_pnl_usdt / margin * 100` حيث `realized_pnl_usdt` تراكمي لكل الأرجل المغلقة والهامش هو **الهامش المبدئي الكامل** — يُضخم ROE للصفقات الجزئية.

**ملاحظة إنصاف**: مجموع `booked_usdt + final_usdt` في USDT متسق ولا يحتوي ازدواجية تدفق نقدي (كل ساق تُخصم من `remaining_qty`، والهامش يُفرج نسبياً، engine.py:3515-3517); الخلل في **الأساس المرجعي للنسب** وليس في النقد.

**الأثر على الدفاعات**: قرارات الرهان/الحماية في `_apply_management` تستخدم `roe` المرفوع، بينما تقييم الحصيلة النهائية في `finalize` يستخدم الأساس الاسمي — عند هامشٍ أكبر يزداد التباعد (e.g. رافعة 10x → ‏"WIN/LOSS" مصنّف على أساس الاسمي عند القفزة "الخاسرة 0.4%" بينما ROE الفعلي −4%). الإصلاح: توحيد الأساس (الاسمي لإعادة PEPER المالية وسجل PERF، والرافعة فقط لحماية ROE الداخلي) أو مضاعفة الاسمي بالرافعة عبر كل المسارات.

---

### E-05 — `sync_position_state` يمسح صفقة أُغلقت خارجياً دون تحصيل PnL (High — يظهر في LIVE)

**الوصف**: عند إغلاق البورصة للمركز خارجياً (مثلاً زناد حد ST وطني أصابه، أو إغلاق يدوي داخل المنصة)، ترى دورة المزامنة أن `fetch_live_snapshot(symbol)` رجع `None` فتستحضر:
```python
STATE["open"] = False
TRADE_STATE["in_position"] = False
_live_manager.lifecycle_state = CLOSED
DASHBOARD_STATE["live_trade_mode"] = False
```
(engine.py:4973-4981) **دون استدعاء `finalize_trade_with_reality`** ولا `_credit_realized_pnl`. فيفقد حساب `PERF["trades"]/wins/losses` إحصاء الصفقة وتختفي تحويلات USDT دون إحصاء. في PAPER (الوضع الحالي) لا يمكن حدوث ذلك لأن الأوامر "الوطنية" لا تنفّذ فعلياً، لكنه خطأ جاهز للانفجار في أول خروج وقفي تصيبه البورصة في LIVE — وهذا تناقض صريح مع "P0-3: سلطة SL يعيشها مدير الصفقة ويقبل نتيجة الجهة" (engine.py:8121-8124) ومع وعد صراط التحقق "paper مجال بروفة للـ LIVE".

**الحل**: عند إغلاق خارجي مكتشف، اتخاذ مسار `finalize_trade_with_reality(symbol)` مع `close_reason="EXTERNAL_CLOSE"` (البيانات من آخر snapshot معروف) قبل إفراغ الحالة.

---

### E-06 — عزل الحالة هشّ وغير قابل للانفكاك عند الاستثناءات + قراءة مباشرة من العمومي (Medium)

**الوصف**:
- `activate`/`deactivate` حول `manage_live_trade` يستخدمان `_capture`/`_blank` (manager.py:110-135) مع نسخ عميق؛ لكن ال pseudocode:
```python
self.activate(symbol)
try:
    ... manage ...
    self._capture()            # في أعقاب النجاح فقط
except Exception:
    log_execution(...)         # manager.py:295-296 — لا يلتقط الحالة
finally:
    self.deactivate()          # manager.py:300
```
عند استثناء في `sync_position_state`/`manage_live_trade` **قبل** `self._capture()` تُفقد تحديثات الدورة، ومع `deactivate` تُفند بالنسخة `_base_state` المتجمدة — لكن سوف تبقى `contexts[symbol]` بالحالة القديمة، فتتكرر الدورة بنفس البيانات دون تراكم تحديث (وضع هادئ) أو تتبقى الحالة العمومية ملوثة لحظياً.
- ال dashboard يقرأ `STATE` مباشرة (dashboard/app.py:1040-1085 وما بعدها) بدل `contexts` الخاصة بالرمّ: أي نافذة زمنية يكون فيها `STATE` يُدار لرمز آخر تظهر فيها بيانات "الموضع الوحيد" كأنها الحالية — وخَط RAW عرض الواجهة.
- `_capture` يخزن `ctx.live_manager = self.engine._live_manager` (manager.py:118) — المرجع نفسه، و`activate` يعيد تحميله (manager.py:159): نسخة واحدة من المثيل تمر عبر كل الرموز بالتناوب، أي أن "عزل" شاد المثيل غير موجود أصلاً؛ فإن آية من one-off له تحديثات (حالة brain, position_profile) تتسرب بين الرموز.

**الحل**: كائن حالة موحد لكل رمز (state + manager + dashboard payload) خارج النواة؛ وال dashboard يُقرأ من `contexts`.

---

### E-07 — `ExecutionService.close(symbol)` يتجاهل الرمز تماماً (Medium)

**الوصف**: الملف `execution/execution_service.py:25-28`:
```python
def close(self, symbol=None):
    if symbol:
        return self.core.close_position_full()
    return self.core.close_position_full()
```
كلا الفرعين متطابقان وكلاهما يغلق "الموجود في STATE" وليس الرمز. لا يوجد أي ربط (`symbol` غير مستخدم). أي متعلق بهذه الواجهة لتطبيق إغلاق متعدد الرموز سيغلق الرمز النشط عشوائياً.

**الحل**: ربط المعلمة ثم توجيه عبر `PortfolioManager.close_symbol(symbol)` أو إفشال المسار إن لم يكن الرمز نشطاً.

---

### E-08 — إحصاءات مضللة: عدد صفقات كل رمز ازداد لكل ساق + نسب مختلطة (Medium)

**الوصف**:
- `_record_partial_leg` (engine.py:8152) → `_credit_realized_pnl` يزيد `ledger["trades"]` لكل ساق إغلاق (engine.py:8142) — صفقة بساقين جزئيتين + نهائي تحسب "3 صفقات" في `PERF["symbols"][sym]["trades"]` بينما `PERF["trades"]` (engine.py:7227) يزيد واحدا فقط عند الإنهاء. تناقض عدّ بين عدّاد عام ومحصّل حسب الرمز يعرضه dashboard.
- نفس المسار يجمع النسب على أساس الاسمي (نقطه E-04) فتراكم `realized_pct` للساق الواحدة دون وزن الحصة.

**الحل**: عدّ "صفقة" حسب `trade_id` فقط (مرة عند `TRADE_CLOSED`)، وجمع النسب موزونة بالحصة.

---

### E-09 — حرجة: كتلة TP1 بلا بوابة سعر؛ إقفال النصف الأول مرتبط بـ `tp1_hold_score` فقط (Critical)

**الوصف**: في `_apply_management` تُنفَّذ كتلة TP1 (engine.py:4731-4778) **دون أي فحص `mark_price >= synthetic_tp1`** — سبقها فقط فحص SL (4724-4729) ثم مباشرة:

```python
if not STATE.get("tp1_hit", False):
    _journal_trade_event(_tj.TP1_ELIGIBLE, ..., reason=f"TP1 profit target arrived (hold_score=...)")  # 4731-4734
    if tp1_hold_score >= 8:
        # delay: فقط يكتب synthetic_tp1 ولا يغلق شيئاً                                  (4739-4744)
    else:
        _ok = close_partial(0.5)                                                        (4748)
```

أي أن القرار الفعلي لإقفال نصف الكمية ليس "وصل السعر إلى الهدف" بل "الدرجة الاستشارية أقل من 8"؛ ورسالة التسجيل تقول "profit target arrived" حتى لو كان السعر بين الدخول والهدف أو تحت الدخول. البحث الشامل يثبت أن `synthetic_tp1`/`dynamic_tp1` لا يُستخدمان كبوابة سعر في أي مكان بالإدارة (مراجع `synthetic_tp1` في engine.py: 4403 عرض، 4744 كتلة الإرجاء، 6306/8105 تعيين، واللوحة) — الهدف المطبوع تجميلي للسلوك الفعلي.

**الأثر (مُلاحظ على أرض الواقع من اللوحة)**: صفقتان BUY حيتان بـ ROE +49.95% (XPL) و +29.24% (WLD) والسعر المتجاوز قيمتي TP1 وTP2 المطبوعتين، ومع ذلك لا TP1 محصّل ولا TP2 أُطلق، والجانب يبقى "Board: ENTRY → MONITOR". السبب التنفيذي: `trade_state` في (TREND_RIDE/EXPANSION...) أو مركّبات الاستمرارية القوية تُبقي `tp1_hold_score >= 8` فلا يُنفَّذ `close_partial(0.5)` أبداً ويبقى الهدف ضبابياً `synthetic_tp1 = dynamic_tp1` بلا مغزى تشغيلي.

**الحل**: إضافة بوابة سعر صريحة قبل كتلة TP1:
```python
if not STATE.get("tp1_hit", False):
    tp1_target = STATE.get("synthetic_tp1") or STATE.get("dynamic_tp1")
    if (side == "BUY" and mark_price >= tp1_target) or (side == "SELL" and mark_price <= tp1_target):
        ... الإرجاء/التنفيذ ...
```
مع توضيح أن "الإرجاء" يجب أن يكون مؤقتاً (نافذة زمنية/خروج أو إجبار تحصيل عند فقد الحالة الصحية)، وإلا صارت "PROFIT_LOCK/DELAY" تأجيلاً أبدياً.

---

### E-10 — أثر مباشر: إغلاق TP2 محجوب خلف `tp1_hit`؛ "سلم" الأهداف معطّل والأهداف المعروضة مضللة (Critical)

**الوصف**: شرط بلوغ TP2 معطّل داخل `if STATE.get("tp1_hit", False):` (engine.py:4780-4815) — أي أن TP2 لا يُفحص أصلاً ما لم تُحصد TP1. وبما أن E-09 يمنع حصد TP1 عند `hold_score >= 8`، فإن **مسارَ إغلاق الأجنحة الكامل (TP1 ثم TP2) يصير رمزاً ميتاً** في أطراف القوة، والحماية العملية المتبقية هي فقط الـ ratchet/trailing.

**العلاقة مع انعكاس العرض TP2 < TP1** (مرصود): السطران المعروضان أظهرا لكلتا الصفقتين:
- XPL BUY entry 0.0875 → TP1 0.0895 (+2.29%) و TP2 0.0893 (+2.06%).
- WLD BUY entry 0.4560 → TP1 0.4748 (+4.12%) و TP2 0.4651 (+2.00%).

السبب: `tp2_price` قبل حصاد TP1 يحمل قيمة قبول العرض الثابتة (+2% من `entry`، engine.py:6043-6044/6045)، بينما `synthetic_tp1`/`dynamic_tp1` معادل به ATR-floor (engine.py:7949-7950 `tp1_atr=2.5`) وربما هدف فحص السائل البعيد — فتعكس الشاشة TP1 أبعد من TP2. في لحظة حصاد TP1 يُعاد حساب TP2 بـ +5%~+15% (engine.py:4789-4799) فيختفي "الانعكاس"، لكن:
- قبل الحصاد لا يوجد أي استخدام لإدارة `synthetic_tp2` أصلاً (مراجعة مراجع `synthetic_tp2`: engine.py:6307/8106 تعيين فقط)؛
- "الانعكاس" على اللوحة يقود المستخدم لاستنتاج غير صحيح بأن الساق الثانية أقرب من الأولى.

**الحل**: إعادة ترتيب السلم ليُحصد TP1 بحاجب سعر، ثم يُفحص TP2 دون شرط `tp1_hit` (شرط مستقل يعتمد `synthetic_tp2`/`tp2_price`)؛ وألا يُطبع `tp2_price` خام ما لم يُعاد حسابه بعد الحصاد — أو طباعة `synthetic_tp2` كهدف الساق الثانية بفرض تناسق TP2 > TP1.

---

## 4) أخطاء تشغيلية ثانوية (Low)

- **L-01** `council_exit` (engine.py:10069) إغلاق إجباري عند `ADX<18` دون سياق زمني (يمكن حدوثه في جلسة الآسيوية الهادئة لصفقة TREND سليمة) — تحقق من نية العمل أثناء النوم.
- **L-02** سياق الصفقة المغلقة يبقى في `contexts` دورةً إضافية واحدة حتى تكشف الدورة التالية `STATE.open=False` (manager.py:267-269) ثم تحذف؛ خلال هذه الدورة قد يظهر في تقرير المحلل نقطة watchlist كـ "EXECUTED" وهو مغلق.
- **L-03** `_live_entry_context` (engine.py:8170) يجسّد محدودية "أفضل جهد": لا يمكنه إعادة ولادة SL/TP إلا إذا توفّر df؛ وعند تعذّر البطء تعود القيمة القديمة من لحظة الرقى مع عدم تعديل — وهو أمر عالق في مسار `runtime._execute_ready_queue_candidate` الذي يمرر `best.stop_loss/tp1/tp2/atr` وقتَ الرقى كمدخل، ويتدبر إعادة الاشتقاق في `execute_entry` فقط إذا نجح الجلب.
- **L-04** في PAPER الخروج النهائي يسعّر بـ `mark` اللحظي (engine.py:7183-7186) ولا يتم "توقف" عند سعر SL/TP — نتيجة الورق تتأخر عن نتيجة ورقة LIVE عند slippage/لقط السوق.

---

## 5) ملخص الجدول

| المعرّف | الخطورة | الطبيعة | الموقع الرئيسي |
|---|---|---|---|
| E-01 | حرجة | سباق خيوط على الحالة العمومية | engine.py (STATE/_live_manager) + manager.py:136-168 + app.py:1388/1421 + bootstrap.py:54-78 |
| E-02 | حرجة | تلوث حافلة عبر الرموز بلا فلتر رمز | engine.py:3819-3832, 3823-3826 |
| E-03 | عالية | قناتي إغلاق متداخلتان | manager.py:279 + engine.py:10066-10083 + engine.py:12006-12011 |
| E-04 | عالية | أساس محاسبة PAPER مختلط (Notional/ROE) | engine.py:4958-4959/3504/7183-7186/8136/8142 |
| E-05 | عالية | فقدان زرع PnL عند إغلاق خارجي في LIVE | engine.py:4973-4981 |
| E-06 | متوسطة | عزل الحالة هشّ + قراءة dashboard مباشرة | manager.py:110-168 + app.py:1040-1085 |
| E-07 | متوسطة | `ExecutionService.close` يتجاهل الرمز | execution/execution_service.py:25-28 |
| E-08 | متوسطة | عدّ صفقات/نسب مختلطة لكل رمز | engine.py:8136-8143, 7227 |
| E-09 | حرجة | كتلة TP1 بلا بوابة سعر (إقفال النصف على `hold_score` فقط) | engine.py:4731-4778 |
| E-10 | حرجة | TP2 محجوب خلف `tp1_hit` + انعكاس عرض TP2<TP1 | engine.py:4780-4815, 4798-4799, 6043-6045 |

---

## 6) توصيات التصحيح مرتبة بالأولوية

1. **الفصل الاحتياطي** (يعالج E-01, E-06): تحويل `STATE`/`TRADE_STATE`/`_live_manager` إلى مثيلات لحظية لكل `PositionContext`، وإبقاء نسخة عرض فقط اختيارية؛ إضافة قفل واحد حول كل إغلاق/فتح/إدارة عبر كل الخيوط.
2. **فلترة الحافلة بالرمز** (E-02) في `_on_reconciled` و`_set_lifecycle` مع إغلاق المصدر عند إزالة الكائن.
3. **بوابة إغلاق واحدة** (E-03): إزالة استدعاء `council_exit` من `manage_all` والاكتفاء بمنطق `_apply_management` + `close_position_full`؛ تعطيل المسار القديم وضبط حارس الإنهاء المزدوج فيه.
4. **توحيد أساس المحاسبة** (E-04, E-08): نِسَب اسمية موزونة بالحصة في `PERF`، و`roe` مرفوع للمدارة فقط؛ عدّ الصفقة مرة واحدة بـ `trade_id`.
5. **تحصيل عند الإغلاق الخارجي** (E-05): فرع إغلاق خارجي يمر عبر `finalize_trade_with_reality`.
6. **ربط الـ execution service** (E-07).
7. **إصلاح سلم الأهداف** (E-09, E-10): بوابة سعر صريحة لـ TP1، وفحص TP2 مستقلاً عن `tp1_hit`، وجعل "الإرجاء" محدوداً بالزمن، وطباعة أهداف متسقة (TP2 > TP1) — والأهم: ألا يكون القرار التشغيلي (الحصاد/الإغلاق) بعيداً عن الأهداف المعروضة.
8. إضافة اختبارات **متعددة الخيوط** (تشغيل `manage_all` مع `/close` في آنٍ) واختبار **سعر STOP عند الخروج الورقي** حتى تُفلصَ هذه الأخطاء تلقائياً.

## 7) ما تم إصلاحه من الجذور (التنفيذ)

تم تنفيذ إصلاحات جذرية جديدة (هذه الجولة — غير تلك الموثقة في التقرير الأصلي)، وكل الاختبارات خضراء:

| المرجع | التغيير | الموضع |
|---|---|---|
| E-09 (حرج) | **بوابة سعر صريحة لـ TP1**: لا يُحصَد النصف الأول إلا بعد أن يبلغ السعر الحيّ `synthetic_tp1` (أو `tp1_price` كبديل)، مهما كان `tp1_hold_score`؛ قبل الوصول للهدف لا يُرفع `TP1_ELIGIBLE` إطلاقاً | `core/engine.py:4731` |
| E-10 (حرج) | **فحص TP2 مستقل عن `tp1_hit`**: سلم الـ runner يُقيَّم كل دورة، الهدف البعيد `synthetic_tp2` ويُدفع دائماً إلى أبعد من TP1، فيُغلق المركز كاملاً عند TP2 حتى لو لم يُحصَد TP1 | `core/engine.py:4798` |
| E-10 (حرج) | **توحيد السلم عند الفلّ**: `_reconcile_levels_after_fill` يفرض لادّر واحداً مرتباً (TP2 أبعد من TP1 للاتجاه) ويحفظ القيم نفسها في كل الحقول (tp1_price/tp2_price/dynamic/synthetic) فلا تنكسر العروض | `core/engine.py:8125` |
| E-10 (حرج) | **الحفاظ على الترتيب بعد الحصاد**: في `_refresh_live_levels` عند `tp1_hit` يُجبر tp2 على البقاء بعد tp1 بمسافة دنيا | `core/engine.py:6323,6329` |
| E-04/E-08 | **محاسبة موزونة الحجم**: كل ساق تضيف نِسبتها من القيمة الاسمية `pnl_usdt/(entry×qty_initial)` بدلاً من نسبة الساق الخام، والصفقة تُعدّ **مرة واحدة** عند الإنهاء (`count_trade=True`) لا لكل ساق | `core/engine.py:8192,8217,8221,8224,7252` |

النتيجة الوظيفية: نفس بيانات المستخدم الآن تعني أن TP1 يُحصد عند وصول السعر لهدفه الحقيقي، وTP2 المعروض دائماً في الجهة البعيدة (TP2 > TP1 للشراء)، والحركة القوية تُغلق في TP2 حتى من دون حصاد intermediate.

التحقق (كل شيء أخضر):
- `tests/test_accounting_lifecycle.py` + `tests/test_trade_management_safety.py`: **44 passed** (توقع `total_pnl_pct` صار موزوناً: 11.0 → 3.5).
- أجنحة إدارة المركز ومحرك الربح (`test_position_management_phase1`, `test_profit_engine_phase3`, `test_fill_reconciliation`, `test_open_timeout_recovery`, `test_atom_management`, `test_radar_position_lifecycle`): **87 passed**.
- أجنحة المحفظة (`test_portfolio_full_cycle`, `test_portfolio_dynamic_6way`, `test_portfolio_isolation`): **10 passed**.
- `tools/six_position_runtime_validation.py`: **SIX_POSITION_RUNTIME_VALIDATION = PASS** — والأدلة تُظهر السلوك الصحيح الجديد: `TP1_ELIGIBLE` لم يعد يُرفع قبل بلوغ السعر، والـ margin invariant محفوظ.
- المجموعة الكاملة: **576 passed, 1 skipped**.

بقي غير منفَّذ بعد (خارج نطاق هذه الجولة): E-01/E-06 (فصل الحالة عمومياً)، E-02 (فلترة الحافلة بالرمز)، E-03 (بوابة إغلاق واحدة)، E-05 (تحصيل الإغلاق الخارجي)، E-07 (ربط الـ execution service بالرمز) — وفق توصيات القسم 6 أعلاه.