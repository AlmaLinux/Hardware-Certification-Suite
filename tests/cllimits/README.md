# Lve and CageFs basic checkers

It is test suite dedicated to check if limits are applied correctly and
is LVE really limited. Also, it tests CageFS by simple check of /etc/passwd.

## CageFS test

Checks if other users are visible for user with enabled CageFS

## Lve limits tests

- CPU: set some speed limit via lvectl, load cpu via stress-ng utility and check via lveps that cpu consumption is limited;
- PMEM: set some pmem limit via lvectl, try to use a bit more memory than was set and check that it is impossible due to limitation;
- NPROC: set some nproc limit via lvectl, try to spawn a bit more processes that was set and check it is impossible due to limitation;
- IO/IOPS: set some io/iops limit via lvectl, try to load io via fio utility and check that consumption was not higher than limit;

Testing of every limit uses own user.