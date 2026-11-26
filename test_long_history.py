import unittest
from continuity import Leases
from formats import Failure
from history_archive import preview

class LongHistory(unittest.TestCase):
    def test_archive_preserves_all_text(self):
        rows=[{'role':'user','content':'x'*350000+'LATE'},{'role':'assistant','content':'done'},{'role':'user','content':'Recall marker'}]; prompt,parts=preview(rows)
        self.assertLess(len(prompt),10000);self.assertIn('Recall marker',prompt);self.assertEqual(''.join(s.split('] ',1)[1] for s in parts),''.join(m['content'] for m in rows))
    def test_lease_conflicts_and_loss(self):
        leases=Leases();key='a'*64
        with leases.acquire(key,'1','m','chat') as a:
            with self.assertRaises(Failure):
                with leases.acquire(key,'2','m','chat'):pass
        with self.assertRaises(Failure):
            with leases.acquire(key,'1','m','chat'):pass
        with leases.acquire(key,'2','m','chat') as b:self.assertIs(a,b)
        with self.assertRaises(RuntimeError):
            with leases.acquire(key,'3','m','chat'):raise RuntimeError()
        with self.assertRaises(Failure):
            with leases.acquire(key,'3','m','chat'):pass
