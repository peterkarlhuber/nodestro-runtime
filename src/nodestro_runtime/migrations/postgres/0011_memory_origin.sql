-- Who wrote a memory file last (N-24; Orkexa's PostgreSQL schema version 2, MemoryEntry.origin): "tool" for the
-- model's memory tool, "user" for a person's explicit write. Every file written before has NULL (origin unknown) and
-- keeps its content and time; adding a nullable column without a default rewrites nothing.
ALTER TABLE nodestro_memory_files ADD COLUMN origin text;
