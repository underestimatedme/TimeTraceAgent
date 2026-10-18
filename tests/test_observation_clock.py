import unittest
from unittest.mock import patch
from timetrace.cloud import CloudClient
from timetrace.agent import Agent
from tests.test_cloud import Response

class ObservationClockTests(unittest.TestCase):
    def client(self, values):
        replies=iter(values)
        return CloudClient('https://qa.example',opener=lambda req,timeout:Response(200,{'code':0,'data':next(replies)}))

    def test_claim_uses_server_clock_and_monotonic_elapsed_despite_wrong_local_clock(self):
        c=self.client([{'server_now':'2026-10-18T10:00:00.123456789Z'}])
        with patch('timetrace.cloud.time.monotonic',return_value=100):
            c.claim('qa')
        with patch('timetrace.cloud.time.monotonic',return_value=103.5), patch('timetrace.cloud.time.time',return_value=1):
            self.assertEqual(c.observed_now(),'2026-10-18T10:00:03.623456Z')
            agent=object.__new__(Agent);agent.cloud=c
            self.assertEqual(agent._observed_now(),c.observed_now())

    def test_renewal_and_out_of_order_responses_never_move_observations_backwards(self):
        c=self.client([{'server_now':'2026-10-18T10:00:00Z'},{'server_now':'2026-10-18T10:00:05Z'}, {'server_now':'2026-10-18T10:00:01Z'}])
        with patch('timetrace.cloud.time.monotonic',return_value=100):c.claim('qa')
        with patch('timetrace.cloud.time.monotonic',return_value=106):
            before=c.observed_now()
            c.renew('qa','attempt',1)
            self.assertGreaterEqual(c.observed_now(),before)
            c.renew('qa','attempt',1)
        with patch('timetrace.cloud.time.monotonic',return_value=108):
            self.assertEqual(c.observed_now(),'2026-10-18T10:00:07.000000Z')

    def test_old_server_and_bad_timestamps_preserve_compatibility(self):
        from datetime import datetime,timezone
        for value in [None,'nonsense','2026-10-18T10:00:00',0]:
            c=self.client([{'server_now':value}])
            c.claim('qa')
            observed=datetime.fromisoformat(c.observed_now().replace('Z','+00:00'))
            self.assertLess(abs((observed-datetime.now(timezone.utc)).total_seconds()),2)
