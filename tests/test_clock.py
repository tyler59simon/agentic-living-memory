"""The record clock (core/clock.py): a headless run may shift it with LIVING_MEMORY_CLOCK_OFFSET_SECONDS; nothing else does."""
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from core import clock,repository,runtime

OFF={'LIVING_MEMORY_HEADLESS':'','CLAUDE_CODE_SESSION_ATTENDED':'1','CLAUDE_CODE_ENTRYPOINT':'cli','LIVING_MEMORY_CLOCK_OFFSET_SECONDS':''}
def env(**kw):return patch.dict('os.environ',{**OFF,**kw})
def gap(moment):return (moment-datetime.now(timezone.utc)).total_seconds()

class Clock(unittest.TestCase):
    def setUp(self):clock._reported.clear()
    def test_the_offset_applies_when_headless(self):
        for headless in ({'LIVING_MEMORY_HEADLESS':'1'},{'CLAUDE_CODE_SESSION_ATTENDED':'0'},{'CLAUDE_CODE_ENTRYPOINT':'sdk-cli'}):
            with self.subTest(headless),env(LIVING_MEMORY_CLOCK_OFFSET_SECONDS='86400',**headless):
                self.assertTrue(clock.simulated());self.assertEqual(clock.offset_seconds(),86400.0)
                self.assertAlmostEqual(gap(clock.now()),86400,delta=5);self.assertEqual(clock.now().tzinfo,timezone.utc)
    def test_a_negative_and_fractional_offset_work(self):
        with env(LIVING_MEMORY_HEADLESS='1',LIVING_MEMORY_CLOCK_OFFSET_SECONDS='-3600.5'):
            self.assertEqual(clock.offset_seconds(),-3600.5);self.assertAlmostEqual(gap(clock.now()),-3600.5,delta=5)
    def test_the_offset_is_ignored_when_not_headless(self):
        with env(LIVING_MEMORY_CLOCK_OFFSET_SECONDS='86400'):
            self.assertFalse(clock.simulated());self.assertEqual(clock.offset_seconds(),0.0);self.assertAlmostEqual(gap(clock.now()),0,delta=5)
    def test_bad_values_are_ignored_and_reported_once(self):
        for bad in ('abc','nan','inf','-inf','1e999','1e30'):
            with self.subTest(bad),env(LIVING_MEMORY_HEADLESS='1',LIVING_MEMORY_CLOCK_OFFSET_SECONDS=bad),patch('core.logs.swallowed') as log:
                self.assertEqual(clock.offset_seconds(),0.0);self.assertFalse(clock.simulated());clock.now();clock.today()
                self.assertEqual(log.call_count,1)
                self.assertAlmostEqual(gap(clock.now()),0,delta=5)
    def test_empty_and_unset_mean_no_offset(self):
        with env(LIVING_MEMORY_HEADLESS='1',LIVING_MEMORY_CLOCK_OFFSET_SECONDS=' '),patch('core.logs.swallowed') as log:
            self.assertFalse(clock.simulated());log.assert_not_called()
        with env(LIVING_MEMORY_HEADLESS='1'):self.assertFalse(clock.simulated())
    def test_today_follows_the_offset_across_midnight(self):
        real=datetime(2026,3,10,23,0,tzinfo=timezone.utc)
        class Fixed(datetime):
            @classmethod
            def now(cls,tz=None):return real.astimezone(tz) if tz else real.replace(tzinfo=None)
        with patch.object(clock,'datetime',Fixed):
            with env(LIVING_MEMORY_HEADLESS='1',LIVING_MEMORY_CLOCK_OFFSET_SECONDS=str(2*3600)):self.assertEqual(clock.today(),(real+timedelta(hours=2)).astimezone().date())
            with env(LIVING_MEMORY_HEADLESS='1',LIVING_MEMORY_CLOCK_OFFSET_SECONDS=str(30*60)):self.assertEqual(clock.today(),(real+timedelta(minutes=30)).astimezone().date())
            with env(LIVING_MEMORY_HEADLESS='1',LIVING_MEMORY_CLOCK_OFFSET_SECONDS=str(-24*3600)):self.assertEqual(clock.today(),(real-timedelta(days=1)).astimezone().date())
            with env(LIVING_MEMORY_CLOCK_OFFSET_SECONDS=str(2*3600)):self.assertEqual(clock.today(),real.astimezone().date())
            with env(LIVING_MEMORY_HEADLESS='1',LIVING_MEMORY_CLOCK_OFFSET_SECONDS=str(86400)):self.assertEqual(clock.today()-real.astimezone().date(),timedelta(days=1))
    def test_record_stamps_and_headless_share_one_environment_test(self):
        with env(LIVING_MEMORY_HEADLESS='1',LIVING_MEMORY_CLOCK_OFFSET_SECONDS='86400'):
            self.assertTrue(runtime.headless())
            stamp=datetime.fromisoformat(repository.now());self.assertAlmostEqual(gap(stamp),86400,delta=5)
        with env():self.assertFalse(runtime.headless())

if __name__=='__main__':unittest.main()
