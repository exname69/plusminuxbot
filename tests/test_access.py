import unittest

from app.access import AccessPolicy, may_dispatch_message


class AccessTests(unittest.TestCase):
    def test_admin_included_even_if_not_in_user_list(self):
        policy = AccessPolicy.from_env({
            "ALLOWED_USER_IDS": "100, 200,100",
            "ADMIN_USER_IDS": "300, 400",
        })
        self.assertEqual(policy.all_user_ids, (100, 200, 300, 400))
        self.assertTrue(policy.is_allowed(100))
        self.assertFalse(policy.is_admin(100))
        self.assertTrue(policy.is_allowed(300))
        self.assertTrue(policy.is_admin(300))
        self.assertFalse(policy.is_allowed(500))
        self.assertFalse(policy.is_admin(500))
        self.assertEqual(policy.notification_admin_id, 300)

    def test_message_access_is_default_deny_except_own_id(self):
        policy = AccessPolicy.from_env({
            "ALLOWED_USER_IDS": "100", "ADMIN_USER_IDS": "300"
        })
        self.assertFalse(may_dispatch_message(policy, 200, "+200 чужое"))
        self.assertFalse(may_dispatch_message(policy, 200, "/stats 100"))
        self.assertFalse(may_dispatch_message(policy, 200, "/start"))
        self.assertFalse(may_dispatch_message(policy, 200, None))
        self.assertTrue(may_dispatch_message(policy, 200, "/id"))
        self.assertTrue(may_dispatch_message(policy, 200, "/id@plusminuxbot"))
        self.assertTrue(may_dispatch_message(policy, 100, "-50"))
        self.assertTrue(may_dispatch_message(policy, 300, "/allstats"))
        self.assertFalse(policy.is_allowed(200))  # callback access is strict

    def test_no_regular_users_works(self):
        policy = AccessPolicy.from_env({"ADMIN_USER_IDS": "123456"})
        self.assertEqual(policy.all_user_ids, (123456,))

    def test_rejects_unsafe_or_incomplete_configuration(self):
        broken = [
            {},
            {"ALLOWED_USER_IDS": "123"},
            {"ADMIN_USER_IDS": ""},
            {"ADMIN_USER_IDS": "0"},
            {"ADMIN_USER_IDS": "-2"},
            {"ADMIN_USER_IDS": "1,"},
            {"ADMIN_USER_IDS": "1,abc"},
            {"ADMIN_USER_IDS": "1", "ALLOWED_USER_IDS": "2,,3"},
        ]
        for env in broken:
            with self.subTest(env=env), self.assertRaises(ValueError):
                AccessPolicy.from_env(env)
