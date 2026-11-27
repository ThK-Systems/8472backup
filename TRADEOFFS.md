# Known Issues & Trade-offs

## Automatic prune after a backward clock correction

With zero snapshot protection and a backward correction of the trusted clock between backups, a newly published snapshot can have an earlier commit time than an existing one. If its protection has expired by the time automatic prune runs and the calendar rule selects the earlier published snapshot, `backup()` can return a snapshot ID that prune has already removed.

## Manual changes to repository files

Manual changes can leave repository data inconsistent. A published snapshot can, for instance, lose its `.catalog` file. Treat an inconsistency detected during an operation as a fatal error (`panic()`) and abort the operation. The backup server administrator handles inspection and repair.
