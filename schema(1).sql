-- ============================================================
-- PropDesk OS — Esquema de Banco de Dados (PostgreSQL)
-- Mesa Proprietária · SaaS Interno do Gestor
-- ============================================================

-- ── Extensões ──────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm"; -- busca fuzzy em e-mails

-- ============================================================
-- 1. TRADERS
-- ============================================================

CREATE TABLE traders (
    id              SERIAL PRIMARY KEY,
    uuid            UUID DEFAULT uuid_generate_v4() UNIQUE NOT NULL,
    name            VARCHAR(150) NOT NULL,
    email           VARCHAR(200) UNIQUE NOT NULL,
    phone           VARCHAR(30),
    document_cpf    VARCHAR(14),                     -- formato: 000.000.000-00
    country         VARCHAR(80) DEFAULT 'Brasil',

    -- Fase e conta
    phase           VARCHAR(10) NOT NULL DEFAULT 'FASE1'
                    CHECK (phase IN ('FASE1', 'FASE2', 'SUSPENDED', 'DISQUALIFIED')),
    account_size    NUMERIC(14,2) NOT NULL,           -- ex: 100000.00
    account_currency VARCHAR(5) DEFAULT 'USD',
    account_number  VARCHAR(50) UNIQUE,               -- identificador na plataforma

    -- KYC
    kyc_status      VARCHAR(30) DEFAULT 'not_submitted'
                    CHECK (kyc_status IN (
                        'not_submitted', 'pending_review',
                        'approved', 'rejected', 'resubmission_required'
                    )),
    kyc_submitted_at TIMESTAMPTZ,
    kyc_approved_at  TIMESTAMPTZ,
    kyc_reviewer     VARCHAR(100),

    -- Drawdown snapshot (atualizado em tempo real via webhook da plataforma)
    dd_daily_current_pct   NUMERIC(6,4) DEFAULT 0,   -- % usado do DD diário
    dd_max_current_pct     NUMERIC(6,4) DEFAULT 0,   -- % usado do DD máximo
    equity_peak_today      NUMERIC(14,2),             -- high-water mark do dia

    -- Metadados
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    notes           TEXT
);

CREATE INDEX idx_traders_email      ON traders(email);
CREATE INDEX idx_traders_phase      ON traders(phase);
CREATE INDEX idx_traders_kyc_status ON traders(kyc_status);

-- ============================================================
-- 2. CONTRATOS (Avaliação Fase 1 / Fondeamento Fase 2)
-- ============================================================

CREATE TABLE contracts (
    id              SERIAL PRIMARY KEY,
    trader_id       INT NOT NULL REFERENCES traders(id) ON DELETE CASCADE,
    phase           VARCHAR(10) NOT NULL CHECK (phase IN ('FASE1', 'FASE2')),

    -- Parâmetros da conta
    account_size    NUMERIC(14,2) NOT NULL,
    profit_target_pct  NUMERIC(5,2) NOT NULL,         -- 8.00 para Fase1, 5.00 para Fase2
    dd_daily_limit_pct NUMERIC(5,2) DEFAULT 5.00,     -- 5%
    dd_max_limit_pct   NUMERIC(5,2) DEFAULT 10.00,    -- 10%
    weekend_hold_allowed BOOLEAN DEFAULT FALSE,

    -- Progresso
    profit_current_pct NUMERIC(6,4) DEFAULT 0,
    status          VARCHAR(30) DEFAULT 'active'
                    CHECK (status IN (
                        'active', 'passed', 'violated_dd_daily',
                        'violated_dd_max', 'expired', 'cancelled'
                    )),

    -- Datas
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,             -- started + 30d (F1) ou 60d (F2)
    completed_at    TIMESTAMPTZ,

    -- Auditoria
    audited_by      VARCHAR(100),
    audit_notes     TEXT,
    audited_at      TIMESTAMPTZ,

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_contracts_trader  ON contracts(trader_id);
CREATE INDEX idx_contracts_status  ON contracts(status);
CREATE INDEX idx_contracts_expires ON contracts(expires_at) WHERE status = 'active';

-- ============================================================
-- 3. EMAILS
-- ============================================================

CREATE TABLE emails (
    id              SERIAL PRIMARY KEY,
    message_id      VARCHAR(300) UNIQUE NOT NULL,    -- ID do Gmail/Outlook
    trader_id       INT REFERENCES traders(id),

    -- Conteúdo
    sender          VARCHAR(200) NOT NULL,
    subject         VARCHAR(500),
    body            TEXT NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL,

    -- Análise IA
    category        VARCHAR(40),                     -- VIOLACAO-DD, PAYOUT, KYC-DOCS...
    urgency         VARCHAR(10)
                    CHECK (urgency IN ('critical', 'high', 'medium', 'low')),
    confidence_score NUMERIC(4,3),                   -- 0.000 – 1.000
    ai_reasoning    TEXT,
    draft_reply     TEXT,                            -- rascunho gerado pela IA
    requires_human_review BOOLEAN DEFAULT TRUE,

    -- Status
    status          VARCHAR(30) DEFAULT 'pending_review'
                    CHECK (status IN (
                        'pending_review', 'auto_queued',
                        'sent', 'rejected', 'archived'
                    )),
    final_reply     TEXT,                            -- resposta final (editada ou não)
    approved_by     VARCHAR(100),
    approved_at     TIMESTAMPTZ,
    sent_at         TIMESTAMPTZ,

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_emails_trader   ON emails(trader_id);
CREATE INDEX idx_emails_status   ON emails(status);
CREATE INDEX idx_emails_urgency  ON emails(urgency);
CREATE INDEX idx_emails_received ON emails(received_at DESC);
CREATE INDEX idx_emails_category ON emails(category);

-- Busca full-text em assunto e corpo
CREATE INDEX idx_emails_subject_trgm ON emails USING GIN (subject gin_trgm_ops);
CREATE INDEX idx_emails_body_trgm    ON emails USING GIN (body gin_trgm_ops);

-- ============================================================
-- 4. TAREFAS (Agenda Inteligente)
-- ============================================================

CREATE TABLE tasks (
    id              SERIAL PRIMARY KEY,
    title           VARCHAR(200) NOT NULL,
    description     TEXT,
    priority        VARCHAR(10) NOT NULL DEFAULT 'medium'
                    CHECK (priority IN ('critical', 'high', 'medium', 'low')),
    category        VARCHAR(40),                     -- PAYOUT, KYC, DD, CONTRATO, REUNIAO...

    -- Vínculos opcionais
    trader_id       INT REFERENCES traders(id),
    source_email_id INT REFERENCES emails(id),

    -- Agenda
    due_date        DATE NOT NULL,
    due_time        TIME,
    status          VARCHAR(20) DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'done', 'cancelled')),
    completed_at    TIMESTAMPTZ,

    -- Metadados
    created_by      VARCHAR(100) DEFAULT 'system',   -- 'system' ou nome do gestor
    assigned_to     VARCHAR(100),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_tasks_due_date  ON tasks(due_date);
CREATE INDEX idx_tasks_priority  ON tasks(priority);
CREATE INDEX idx_tasks_status    ON tasks(status);
CREATE INDEX idx_tasks_trader    ON tasks(trader_id);

-- ============================================================
-- 5. PAYOUTS (Solicitações de Saque)
-- ============================================================

CREATE TABLE payout_requests (
    id              SERIAL PRIMARY KEY,
    trader_id       INT NOT NULL REFERENCES traders(id) ON DELETE CASCADE,
    contract_id     INT REFERENCES contracts(id),

    -- Valores
    amount          NUMERIC(14,2) NOT NULL,
    currency        VARCHAR(5) DEFAULT 'USD',
    method          VARCHAR(30)
                    CHECK (method IN ('PIX', 'WIRE', 'USDT', 'OTHER')),
    pix_key         VARCHAR(200),
    wallet_address  VARCHAR(200),

    -- Status
    status          VARCHAR(30) DEFAULT 'pending'
                    CHECK (status IN (
                        'pending', 'under_review', 'approved',
                        'processing', 'paid', 'rejected'
                    )),

    -- Auditoria
    requested_at    TIMESTAMPTZ DEFAULT NOW(),
    reviewed_by     VARCHAR(100),
    reviewed_at     TIMESTAMPTZ,
    paid_at         TIMESTAMPTZ,
    rejection_reason TEXT,
    notes           TEXT
);

CREATE INDEX idx_payouts_trader  ON payout_requests(trader_id);
CREATE INDEX idx_payouts_status  ON payout_requests(status);
CREATE INDEX idx_payouts_date    ON payout_requests(requested_at DESC);

-- ============================================================
-- 6. VIOLAÇÕES DE DRAWDOWN
-- ============================================================

CREATE TABLE dd_violations (
    id              SERIAL PRIMARY KEY,
    trader_id       INT NOT NULL REFERENCES traders(id),
    contract_id     INT REFERENCES contracts(id),

    violation_type  VARCHAR(20) NOT NULL
                    CHECK (violation_type IN ('daily', 'max_trailing')),

    -- Snapshot no momento da violação
    equity_at_violation NUMERIC(14,2),
    equity_peak         NUMERIC(14,2),
    amount_breached     NUMERIC(14,2),              -- valor em $ da violação
    pct_breached        NUMERIC(6,4),               -- % excedido

    occurred_at     TIMESTAMPTZ DEFAULT NOW(),

    -- A conta foi encerrada automaticamente?
    auto_closed     BOOLEAN DEFAULT TRUE,
    platform_ref    VARCHAR(200)                    -- referência na plataforma de trading
);

CREATE INDEX idx_violations_trader   ON dd_violations(trader_id);
CREATE INDEX idx_violations_occurred ON dd_violations(occurred_at DESC);

-- ============================================================
-- 7. EVENTOS DE MERCADO (para priorização da agenda)
-- ============================================================

CREATE TABLE market_events (
    id              SERIAL PRIMARY KEY,
    event_name      VARCHAR(100) NOT NULL,           -- NFP, FOMC, CPI, GDP...
    event_date      DATE NOT NULL,
    event_time_est  TIME,
    impact_level    VARCHAR(10) NOT NULL
                    CHECK (impact_level IN ('high', 'medium', 'low')),
    currency        VARCHAR(10),                     -- USD, EUR, BRL...
    description     TEXT,

    UNIQUE (event_name, event_date)
);

-- Seed com eventos recorrentes
INSERT INTO market_events (event_name, event_date, event_time_est, impact_level, currency) VALUES
('NFP - Non-Farm Payroll', CURRENT_DATE + (5 - EXTRACT(DOW FROM CURRENT_DATE)::int)::int, '09:30', 'high', 'USD'),
('CPI - Inflação EUA', CURRENT_DATE + 7, '09:30', 'high', 'USD'),
('FOMC - Decisão de Juros', CURRENT_DATE + 14, '14:00', 'high', 'USD');

-- ============================================================
-- 8. CONFIGURAÇÕES DA MESA
-- ============================================================

CREATE TABLE desk_settings (
    id              SERIAL PRIMARY KEY,
    key             VARCHAR(100) UNIQUE NOT NULL,
    value           JSONB NOT NULL,
    description     TEXT,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_by      VARCHAR(100)
);

INSERT INTO desk_settings (key, value, description) VALUES
('rules.dd_daily_pct',     '5.0',                              'Drawdown diário máximo (%)'),
('rules.dd_max_pct',       '10.0',                             'Drawdown máximo trailing (%)'),
('rules.phase1_target',    '8.0',                              'Meta de lucro Fase 1 (%)'),
('rules.phase2_target',    '5.0',                              'Meta de lucro Fase 2 (%)'),
('rules.phase1_days',      '30',                               'Prazo máximo Fase 1 (dias)'),
('rules.phase2_days',      '60',                               'Prazo máximo Fase 2 (dias)'),
('payout.processing_days', '3',                                'SLA de processamento de payout (dias úteis)'),
('kyc.processing_days',    '2',                                'SLA de revisão KYC (dias úteis)'),
('ai.auto_send_threshold', '0.90',                             'Confiança mínima para envio automático de e-mail'),
('email.retry_discount',   '"RETRY20"',                        'Código de desconto após violação'),
('desk.name',              '"PropDesk Trading"',               'Nome da mesa para e-mails'),
('desk.support_email',     '"suporte@propdesk.com"',           'E-mail de suporte');

-- ============================================================
-- 9. TRIGGERS — atualização automática de updated_at
-- ============================================================

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_traders_updated
    BEFORE UPDATE ON traders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_tasks_updated
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- 10. VIEW — dashboard matinal do gestor
-- ============================================================

CREATE OR REPLACE VIEW v_daily_manager_briefing AS
SELECT
    'payout'   AS alert_type,
    pr.id      AS ref_id,
    t.name     AS trader_name,
    t.id       AS trader_id,
    t.phase,
    ('Payout de $' || pr.amount || ' aguardando aprovação')::TEXT AS description,
    'high'     AS urgency,
    pr.requested_at AS event_at

FROM payout_requests pr
JOIN traders t ON t.id = pr.trader_id
WHERE pr.status = 'pending'
  AND pr.requested_at::date = CURRENT_DATE

UNION ALL

SELECT
    'dd_violation',
    v.id,
    t.name,
    t.id,
    t.phase,
    'Violação de ' || v.violation_type || ' DD — $' || v.amount_breached,
    'critical',
    v.occurred_at

FROM dd_violations v
JOIN traders t ON t.id = v.trader_id
WHERE v.occurred_at >= NOW() - INTERVAL '24 hours'

UNION ALL

SELECT
    'kyc_pending',
    t.id,
    t.name,
    t.id,
    t.phase,
    'KYC aguardando revisão há ' ||
        EXTRACT(DAY FROM NOW() - t.kyc_submitted_at)::int || ' dias',
    CASE
        WHEN t.kyc_submitted_at < NOW() - INTERVAL '3 days' THEN 'high'
        ELSE 'medium'
    END,
    t.kyc_submitted_at

FROM traders t
WHERE t.kyc_status = 'pending_review'

UNION ALL

SELECT
    'expiring_contract',
    c.id,
    t.name,
    t.id,
    t.phase,
    c.phase || ' expira em ' ||
        EXTRACT(HOUR FROM c.expires_at - NOW())::int || 'h — Meta: ' ||
        c.profit_current_pct || '% / ' || c.profit_target_pct || '%',
    'high',
    c.expires_at

FROM contracts c
JOIN traders t ON t.id = c.trader_id
WHERE c.status = 'active'
  AND c.expires_at BETWEEN NOW() AND NOW() + INTERVAL '48 hours'

ORDER BY
    CASE urgency WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
    event_at DESC;
