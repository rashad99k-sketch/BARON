import importlib
import os
import sys
import types
import unittest

class _FakeFlask:
    def __init__(self,*a,**k): pass
    def route(self,*a,**k): return lambda fn: fn
    def add_url_rule(self,*a,**k): return None

def _load():
    saved={k:sys.modules.get(k) for k in ('ccxt','flask','core.engine')}
    old=os.environ.pop('PAPER_MODE',None)
    ccxt=types.ModuleType('ccxt')
    class FakeBingX:
        def __init__(self,*a,**k): self.markets={}
    ccxt.bingx=FakeBingX
    flask=types.ModuleType('flask'); flask.Flask=_FakeFlask; flask.jsonify=lambda *a,**k:None; flask.request=types.SimpleNamespace()
    sys.modules['ccxt']=ccxt; sys.modules['flask']=flask; sys.modules.pop('core.engine',None)
    return importlib.import_module('core.engine'),saved,old

class EarlyPreparedQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.E,cls.saved,cls.old=_load()
    @classmethod
    def tearDownClass(cls):
        sys.modules.pop('core.engine',None)
        for k,v in cls.saved.items():
            if v is None: sys.modules.pop(k,None)
            else: sys.modules[k]=v
        if cls.old is not None: os.environ['PAPER_MODE']=cls.old

    def setUp(self):
        E=self.E
        E.MEMORY['watchlist']={}
        E.MEMORY['institutional_zone_analysis']={}
        E.queue._candidates.clear()

    def test_precursor_cluster_is_prepared_without_a_grade(self):
        E=self.E
        radar=E.InstitutionalRadar()
        entry={
            'symbol':'TEST/USDT:USDT','strength':'MEDIUM','deep_analyzed':True,
            'institutional_zone_active':True,
            'pre_institutional_state':'PRE_EXPANSION_LONG',
            'pre_expansion_state':'PRE_EXPANSION_LONG',
            'pre_expansion':{'phase':'EARLY_EXPANSION','indicator_alignment':'BULLISH'},
            'pre_expansion_evidence':['DISPLACEMENT','REJECTION','MSB_MSS'],
            'analysis':{'ob_grade':'B','roro_signal':False,'struct_score':55,'liq_score':45,'trap_risk':20},
        }
        ready=radar._update_a_grade_status(entry)
        self.assertFalse(ready)
        self.assertTrue(entry['institutional_prepared'])
        self.assertFalse(entry['a_grade_ready'])
        self.assertEqual(entry['institutional_stage'],'PREPARED_FOR_ENTRY')

    def test_prepared_candidate_is_admitted_to_queue(self):
        E=self.E
        E.MEMORY['watchlist']={'TEST/USDT:USDT':{
            'symbol':'TEST/USDT:USDT','side':'BUY','strength':'MEDIUM','deep_analyzed':True,
            'institutional_zone_active':True,'institutional_prepared':True,'a_grade_ready':False,
            'institutional_prepared_reasons':['DISPLACEMENT','REJECTION','MSB_MSS'],
            'pre_expansion':{'phase':'EARLY_EXPANSION'},'analysis':{'price':100.0,'atr':1.0,'side':'BUY'},
            'trade_type':'REVERSAL','trade_style':'SCALP','entry_timing':'RETEST_ENTRY',
        }}
        E.MEMORY['institutional_zone_analysis']={'TEST/USDT:USDT':{
            'symbol':'TEST/USDT:USDT','side':'BUY','institutional_score':70,'composite_score':60,'precursor_count':3}}
        n=E.promote_to_queue()
        self.assertEqual(n,1)
        cand=E.queue._candidates['TEST/USDT:USDT']
        self.assertFalse(cand.is_a_grade)
        self.assertIn('PREPARED:', cand.decision_reasons[0])

if __name__=='__main__': unittest.main()
