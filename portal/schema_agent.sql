-- Added for the assistant pilot. The department's schema.sql is untouched: this
-- file is applied after it, and every statement is idempotent.
--
-- The agent channel needs one thing the web form does not: a way to remember a
-- document that arrived over the assistant API before the application that uses
-- it exists. Keeping the reference in the database rather than in the MCP
-- server means the assistant holds no state of its own, and it means the
-- department can see afterwards exactly which documents came in through which
-- channel.

CREATE TABLE IF NOT EXISTS agent_documents (
    ref        VARCHAR(32) PRIMARY KEY,
    mobile     VARCHAR(15),
    filename   VARCHAR(200),
    path       VARCHAR(300),
    created_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_documents_mobile ON agent_documents(mobile);
