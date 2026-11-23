# Test purchase-order files

Upload on **Connections** (Excel, first sheet). Each file also has an *About this file* sheet saying what it tests.
Due dates count from **01 Nov 2026**, the app's today (the day after the last stock count).

| File | Orders | What it tests |
|---|---|---|
| po-01-routine-small.xlsx | 8 | Small, later orders: absorbed, no re-plan |
| po-02-urgent-high-priority.xlsx | 4 | Urgent high priority: immediate re-plan |
| po-03-large-overload.xlsx | 12 | More than capacity: the demand and capacity decision |
| po-04-mixed-week.xlsx | 25 | A normal mixed week |
| po-05-bulk-eight-weeks.xlsx | 60 | Eight weeks of distributor orders: weekly build plan, rough-cut |
| po-06-update-mixed-week.xlsx | 10 | Updates to po-04 (upload po-04 first) |
| po-07-invalid-rows.xlsx | 9 | Every row invalid: whole file refused, problems listed |
| po-08-header-aliases.xlsx | 6 | Other column names, no channel/price |
| po-09-single-product-surge.xlsx | 10 | One product surges: material shortage, jobs wait for parts |

Order numbers start T01- to T09- so they never clash with the order book. Uploading a file again updates its orders,
unless they are already in an approved plan (frozen), which is refused.
