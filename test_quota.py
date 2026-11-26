import unittest
from deploy.quota import quota_rows


class QuotaRows(unittest.TestCase):
    def test_display_name_is_not_model_identity(self):
        models={k:{'displayName':'Gemini 3.5 Flash Lite','quotaInfo':{'remainingFraction':.9671297}} for k in ('gemini-2.5-flash','gemini-3.5-flash-lite')}
        rows=quota_rows({}, {'models':models})
        self.assertEqual([r['name'] for r in rows],sorted(models))
        self.assertTrue(all(r['kind']=='model' and r['remaining']==96.7 for r in rows))

    def test_bucket_keeps_scope_and_unknown_is_not_zero(self):
        group={'displayName':'Claude and GPT models','buckets':[dict(displayName='Weekly',bucketId='3p-weekly',resetTime='2099-01-01')]}
        r,=quota_rows({'groups':[group]},None)
        self.assertEqual((r['kind'],r['label'],r['remaining']),('bucket','Claude and GPT models · Weekly',None))
