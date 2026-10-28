Title: Fix test regression from deposit rework and neutralize legacy deposit paths

At commit 617feb9 tests_tron ran 52 tests, all passing. Now it runs 39 with 1 failure and 16 errors. This is a regression, not legacy.

1. Run `git diff 617feb9 --name-status` and list every deleted or heavily reduced file with the reason. Restore anything unrelated to DepositRequest.
2. Restore the 13 missing tests and fix the 17 failures at the root cause. Do not disable or delete tests.
3. Show the production guard on TopUpAPIView / demo funding. It must fail CLOSED when its setting is missing. Add a test.
4. Make scan_tron_deposits and tron_monitor_status refuse to run unless ENABLE_LEGACY_TRON_SCAN=true (default off).
5. List every code path that writes DemoAccount.cash_balance or creates deposit/credit Transactions, with file and line.
6. Create docs/deposits-api.md documenting /api/deposits/, /api/deposits/config/, /api/admin/deposits/ and its action endpoint, and /api/admin/users/<pk>/deposits/ (fields, errors).
7. In the PR description, report test results for every module.
