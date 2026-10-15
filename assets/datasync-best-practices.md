# AWS DataSync Best Practices for NFS to Amazon S3 Migrations

This document is the source material for the MigrationBot knowledge base. It
summarizes practical guidance for planning, running, and finishing a migration
from an NFS file share to Amazon S3 with AWS DataSync. For authoritative
details, see the AWS DataSync User Guide.

## 1. Planning a migration

- Inventory the source first: total size, number of files and directories,
  largest directories, and how quickly the data changes.
- Decide on the destination layout (bucket, prefix, storage class) before the
  first transfer. DataSync writes the source directory structure under the
  destination prefix.
- Plan for one full baseline transfer, one or more incremental transfers, and
  a short final cutover. Do not plan a single copy as the whole migration for
  data that is still being written to.
- Agree a rollback window in advance. Decide how long the source stays
  available after cutover.

## 2. Deploying and activating the agent

- Deploy the DataSync agent as close to the NFS server as possible, on the same
  network, to keep latency low and to avoid sending NFS traffic across slow or
  shared links.
- Size the agent for the data set. Larger file counts need more memory and
  vCPUs, so follow the current DataSync agent requirements for your file count.
- The agent must reach the DataSync service endpoint over HTTPS (TCP 443). When
  possible, use a VPC interface endpoint so agent traffic stays on the AWS
  network.
- Activation requires temporary access to the agent on TCP 80 to retrieve an
  activation key. Restrict this access to the address of the person activating
  the agent, and remove it afterwards.
- Activate the agent in the same AWS Region as the destination and confirm the
  agent shows an ONLINE status before creating locations.

## 3. Locations and permissions

- Create the NFS source location with the agent and the exact export path you
  intend to migrate. Use the narrowest subdirectory that covers the data.
- Create the Amazon S3 destination location with a dedicated IAM role that can
  only access the destination bucket. Do not reuse broad administrator roles.
- Grant the NFS export read access for the agent's address only.
- Keep the destination bucket private, encrypted, and with public access
  blocked. Enable versioning when you want protection against accidental
  overwrites.

## 4. Task configuration

- Use the default transfer mode that copies only data that has changed. This
  makes the baseline and every later run efficient, because unchanged files are
  skipped.
- Enable CloudWatch Logs for the task so that you can see which files were
  transferred or failed. Use an appropriate log level for the size of the
  migration.
- Choose a verification mode deliberately. Verifying only the files that were
  transferred is faster, and verifying the whole destination is more thorough
  but slower on large data sets.
- Use include and exclude filters to leave out temporary files, caches, or
  directories that do not belong in the destination.
- Use the bandwidth limit option if the migration shares a network link with
  production traffic.
- Preserve the metadata you need, such as timestamps and permissions, and
  review how POSIX metadata is stored in S3 before relying on it.

## 5. Running and monitoring a transfer

- A task execution moves through these statuses: LAUNCHING, PREPARING,
  TRANSFERRING, VERIFYING, and SUCCESS. A status of ERROR means the execution
  failed and the logs should be reviewed.
- Poll the execution status at a sensible interval instead of continuously. For
  small data sets an execution can finish in minutes, and for large data sets it
  can take hours or days.
- Compare files transferred and bytes transferred against the source inventory
  when the execution finishes.
- Review CloudWatch Logs for files that failed to transfer, and fix the cause
  (permissions, file names, locks) before the next run.
- If an execution fails or is cancelled, start a new execution of the same
  task. Because only changed data is copied, it continues efficiently.

## 6. Best practices for a final cutover migration

A cutover is the moment applications stop using the source storage and start
using the destination. Follow this sequence:

1. Complete at least one full baseline transfer well before the cutover date.
   This moves the bulk of the data while the source is still in use.
2. Run incremental transfers on a regular schedule to synchronize the changes
   made since the baseline. Each run gets shorter because only changes are
   copied. Use the time of the last incremental run to estimate how long the
   final sync will take.
3. Schedule a maintenance window for the cutover and tell the application
   owners.
4. Stop write operations to the source storage before the final sync. Unmount
   the share or make it read-only so that no new changes appear after the final
   transfer starts.
5. Run the final incremental sync and wait for the task execution to reach
   SUCCESS.
6. Verify that file counts and total sizes match between the source and the
   destination, and spot-check important files.
7. Switch applications and users to the new location in Amazon S3.
8. Keep the source data available, unchanged, for rollback for 24 to 48 hours
   (or your agreed rollback window, if it is longer) and until you have
   confirmed that the migrated data works for the applications that use it.
   Decommission the source only after that window ends.

## 7. Verification checklist

- The final task execution status is SUCCESS.
- The number of files and the total bytes at the destination match the source
  inventory.
- No unresolved errors remain in the task's CloudWatch Logs.
- Application owners have confirmed that the data is readable and complete.
- The source remains available for rollback until sign-off.

## 8. Security and cleanup

- Use least-privilege IAM roles for DataSync and for any automation that
  creates locations and tasks.
- Close temporary network access that was needed for activation.
- When the migration is complete, delete tasks and locations that are no longer
  needed, and deactivate or delete the agent.
