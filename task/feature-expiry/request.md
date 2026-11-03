Feature request: optional expiry date for stock batches.

Some stock can no longer be sold after a certain date. Add an optional expiry
date to batches, and stop allocating order lines to batches that have expired.

Acceptance criteria:

1. The CreateBatch command accepts an optional `expiry` date (default None). A
   Batch has an `expiry` attribute with the same default. Every existing way of
   creating them, without an expiry, must keep working unchanged.
2. The expiry date is saved in the database with the batch. After loading a
   batch again through a new unit of work, its `expiry` has the same value, or
   None when no expiry was given.
3. A batch with an expiry date in the past cannot allocate. A batch can still
   allocate on its expiry date itself. Allocation skips expired batches and uses
   the next suitable one. If only expired batches have stock, allocation behaves
   as out of stock.
4. Creating a batch with an invalid expiry must fail with a clear error:
   add an exception class `InvalidExpiry` next to the existing `InvalidSku`, and
   raise it, naming the batch reference in the message, when the expiry is not a
   date, or when it is earlier than the batch's eta. Nothing may be saved then.
   An expiry equal to the eta is valid.

Find the modules this involves yourself. Do not change tests or files outside
src/allocation/. When you think the change is complete, reply with final. The
harness then runs the checks and tells you about any failure.
