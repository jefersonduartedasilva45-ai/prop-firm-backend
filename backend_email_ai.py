"""
PropDesk OS — Backend Principal
FastAPI + IA para Processamento de E-mails da Mesa Proprietária

Stack: Python 3.11 · FastAPI · PostgreSQL (asyncpg) · Anthropic Claude API
"""

import os
import asyncpg
import anthropic
from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime, date
from enum import Enum
import json
import re

# ---------------------------------------------------------------------------
# App & config
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PropDesk OS — API",
    description="SaaS interno para gestão de Prop Firm",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", os.getenv("FRONTEND_URL", "")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AI_CLIENT = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
DB_URL = os.environ["DATABASE_URL"]  # postgresql+asyncpg://user:pass@host/db

# ---------------------------------------------------------------------------
# Enums & Schemas
# ---------------------------------------------------------------------------

class EmailCategory(str, Enum):
    VIOLATION_DD     = "VIOLACAO-DD"
    PAYOUT_REQUEST   = "PAYOUT"
    KYC_DOCS         = "KYC-DOCS"
    SLIPPAGE         = "SLIPPAGE"
    RULES_FAQ        = "REGRAS-FAQ"
    CONTRACT_PHASE   = "CONTRATO-FASE"
    DISPUTE          = "DISPUTA"
    OTHER            = "OUTRO"

class UrgencyLevel(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"

class TaskPriority(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"

class InboundEmail(BaseModel):
    message_id: str
    sender: EmailStr
    subject: str
    body: str
    received_at: datetime

class EmailAnalysisResult(BaseModel):
    category: EmailCategory
    urgency: UrgencyLevel
    trader_id: Optional[str]
    confidence_score: float           # 0.0 – 1.0
    reasoning: str
    draft_reply: str
    requires_human_review: bool
    create_task: bool
    task_description: Optional[str]
    task_priority: Optional[TaskPriority]

class ApproveEmailRequest(BaseModel):
    email_id: int
    final_reply: str                  # pode ser editado pelo gestor
    approved_by: str

class TaskCreate(BaseModel):
    title: str
    description: str
    priority: TaskPriority
    due_date: date
    category: str
    trader_id: Optional[str] = None

# ---------------------------------------------------------------------------
# Sistema de regras da mesa — carregado uma vez
# ---------------------------------------------------------------------------

MESA_RULES = """
=== REGRAS DA MESA PROPRIETÁRIA ===

DRAWDOWN DIÁRIO:
- Limite: 5% do equity de pico do dia (não do saldo inicial)
- O "equity de pico" é o maior valor de equity registrado no dia corrente
- Exemplo: conta $100K, equity chegou a $103K → limite de perda diária = $5.150
- Violação: encerramento automático e irreversível da conta
- Cláusula: Seção 3.2 do Contrato de Avaliação

DRAWDOWN MÁXIMO (TRAILING):
- Limite: 10% do saldo inicial da conta
- Calculado sobre saldo inicial, não sobre equity atual
- Conta $100K → perda máxima acumulada permitida = $10.000
- Cláusula: Seção 3.1 do Contrato de Avaliação

META DE LUCRO:
- Fase 1: 8% do saldo inicial
- Fase 2: 5% do saldo inicial
- Prazo máximo: Fase 1 = 30 dias corridos · Fase 2 = 60 dias corridos

POSIÇÕES:
- Proibido manter posições abertas durante o final de semana
- Fechamento obrigatório: sexta-feira 17:00 EST
- Proibido operar durante eventos de notícia de Impacto Alto (Cláusula 6.1)

SLIPPAGE E EXECUÇÃO:
- Slippage em eventos de alto impacto (NFP, FOMC, CPI) não gera ressarcimento
- Previsto em Condições de Mercado — Seção 5.1
- Slippage anormal fora de eventos pode ser analisado em até 3 dias úteis

PAYOUTS (SAQUES):
- Prazo de processamento: 2-3 dias úteis após aprovação
- Aprovação requer: meta de lucro atingida + auditoria de drawdown ok + KYC aprovado
- Métodos: PIX (Brasil), Wire Transfer, Crypto (USDT)

KYC:
- Documentos aceitos: RG/CNH frente e verso + CPF + comprovante de residência + selfie
- Prazo de análise: 1-2 dias úteis após envio completo
- KYC reprovado: trader pode reenviar em 5 dias

FASES:
- Fase 1: conta de avaliação · meta 8% · dd-diario 5% · dd-max 10%
- Fase 2: conta fondeada · meta 5% · dd-diario 5% · dd-max 10%
- Aprovação Fase 1→2: meta atingida + auditoria humana + KYC aprovado
- Conta Fase 2 é conta real com capital da mesa

CÓDIGO DE DESCONTO RETRY: RETRY20 (20% off nova avaliação após violação)
"""

# ---------------------------------------------------------------------------
# Prompt de sistema para classificação e geração de resposta
# ---------------------------------------------------------------------------

CLASSIFIER_SYSTEM_PROMPT = f"""
Você é o sistema de IA interno de uma Mesa Proprietária de Trading (Prop Firm).
Sua função é analisar e-mails recebidos de traders, classificá-los e gerar
respostas precisas baseadas estritamente nas regras da mesa.

{MESA_RULES}

Você deve retornar SEMPRE um JSON válido com esta estrutura exata:
{{
  "category": "<categoria>",
  "urgency": "<urgency>",
  "trader_id": "<id ou null>",
  "confidence_score": <0.0 a 1.0>,
  "reasoning": "<raciocínio resumido em 1 frase>",
  "draft_reply": "<resposta completa em português, formal mas direta>",
  "requires_human_review": <true/false>,
  "create_task": <true/false>,
  "task_description": "<descrição da tarefa ou null>",
  "task_priority": "<critical|high|medium|low ou null>"
}}

Categorias disponíveis:
- VIOLACAO-DD: violação de drawdown diário ou máximo
- PAYOUT: solicitação ou dúvida sobre saques
- KYC-DOCS: envio ou dúvidas sobre documentos KYC
- SLIPPAGE: reclamação sobre execução ou slippage
- REGRAS-FAQ: dúvidas gerais sobre regras e funcionamento
- CONTRATO-FASE: dúvidas sobre Fase 1/2, contratos, metas
- DISPUTA: contestação de encerramento de conta
- OUTRO: qualquer outro assunto

Critérios de urgência:
- critical: violação de DD, disputa de encerramento, payout atrasado >5 dias
- high: novo payout solicitado, KYC aguardando >3 dias, contrato expirando em <48h
- medium: dúvidas sobre regras, KYC recém-enviado, slippage em evento programado
- low: FAQ simples, solicitações que já foram respondidas, informações gerais

Critérios para requires_human_review=true:
- Confiança abaixo de 0.85
- Disputas de encerramento complexas
- Alegações de erro técnico do sistema
- Valores de payout acima de $10.000

Critérios para create_task=true:
- Payout que requer aprovação manual
- KYC que requer revisão de compliance
- Auditoria necessária

Responda APENAS com o JSON. Sem texto extra, sem markdown, sem explicações fora do JSON.
"""

# ---------------------------------------------------------------------------
# Dependency: DB connection
# ---------------------------------------------------------------------------

async def get_db():
    conn = await asyncpg.connect(DB_URL)
    try:
        yield conn
    finally:
        await conn.close()

# ---------------------------------------------------------------------------
# Core endpoint: processar e-mail recebido (webhook Gmail/Outlook)
# ---------------------------------------------------------------------------

@app.post("/api/v1/emails/inbound", response_model=dict)
async def process_inbound_email(
    email: InboundEmail,
    background_tasks: BackgroundTasks,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Webhook principal. Recebe e-mail bruto (via Gmail Push / Outlook webhook),
    aciona IA para classificação + geração de resposta, persiste no banco
    e decide automaticamente se envia ou coloca na fila de aprovação humana.
    """

    # 1) Buscar trader pelo e-mail remetente
    trader = await db.fetchrow(
        "SELECT id, name, phase, account_size, kyc_status FROM traders WHERE email = $1",
        str(email.sender),
    )

    trader_context = ""
    if trader:
        trader_context = (
            f"\nContexto do trader no sistema:\n"
            f"- ID: {trader['id']}\n"
            f"- Nome: {trader['name']}\n"
            f"- Fase atual: {trader['phase']}\n"
            f"- Tamanho da conta: ${trader['account_size']:,.0f}\n"
            f"- Status KYC: {trader['kyc_status']}\n"
        )

    # 2) Chamar IA para análise e geração de resposta
    user_message = (
        f"Assunto: {email.subject}\n\n"
        f"Corpo do e-mail:\n{email.body}\n"
        f"{trader_context}"
    )

    ai_response = AI_CLIENT.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        system=CLASSIFIER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_json = ai_response.content[0].text.strip()
    # Remover possíveis backticks se o modelo os incluir
    raw_json = re.sub(r"^```json\s*|\s*```$", "", raw_json, flags=re.MULTILINE)

    try:
        analysis = EmailAnalysisResult(**json.loads(raw_json))
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"IA retornou JSON inválido: {e}")

    # 3) Persistir e-mail + análise no banco
    email_id = await db.fetchval(
        """
        INSERT INTO emails (
            message_id, sender, subject, body, received_at,
            category, urgency, confidence_score, draft_reply,
            requires_human_review, trader_id, ai_reasoning, status
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        RETURNING id
        """,
        email.message_id,
        str(email.sender),
        email.subject,
        email.body,
        email.received_at,
        analysis.category.value,
        analysis.urgency.value,
        analysis.confidence_score,
        analysis.draft_reply,
        analysis.requires_human_review,
        trader["id"] if trader else None,
        analysis.reasoning,
        "pending_review" if analysis.requires_human_review else "auto_queued",
    )

    # 4) Criar tarefa na agenda se necessário
    if analysis.create_task and analysis.task_description:
        today = date.today()
        await db.execute(
            """
            INSERT INTO tasks (
                title, description, priority, due_date,
                category, trader_id, source_email_id
            ) VALUES ($1,$2,$3,$4,$5,$6,$7)
            """,
            analysis.task_description[:120],
            f"Criado automaticamente a partir do e-mail #{email_id}",
            analysis.task_priority.value if analysis.task_priority else "medium",
            today,
            analysis.category.value,
            trader["id"] if trader else None,
            email_id,
        )

    # 5) Se confiança alta e não precisa de revisão: envio automático em background
    if not analysis.requires_human_review and analysis.confidence_score >= 0.90:
        background_tasks.add_task(
            send_auto_reply,
            recipient=str(email.sender),
            subject=f"Re: {email.subject}",
            body=analysis.draft_reply,
            email_id=email_id,
        )
        action = "auto_sent"
    else:
        action = "queued_for_review"

    return {
        "email_id": email_id,
        "action": action,
        "category": analysis.category.value,
        "urgency": analysis.urgency.value,
        "confidence": analysis.confidence_score,
        "requires_review": analysis.requires_human_review,
        "task_created": analysis.create_task,
    }


# ---------------------------------------------------------------------------
# Endpoint: aprovar e enviar rascunho (ação do gestor no painel)
# ---------------------------------------------------------------------------

@app.post("/api/v1/emails/{email_id}/approve")
async def approve_and_send(
    email_id: int,
    payload: ApproveEmailRequest,
    background_tasks: BackgroundTasks,
    db: asyncpg.Connection = Depends(get_db),
):
    """Gestor aprova (ou edita) o rascunho da IA e dispara o envio."""
    row = await db.fetchrow(
        "SELECT sender, subject, status FROM emails WHERE id = $1", email_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="E-mail não encontrado")
    if row["status"] in ("sent", "rejected"):
        raise HTTPException(status_code=409, detail=f"E-mail já foi {row['status']}")

    await db.execute(
        """
        UPDATE emails
        SET status = 'sent',
            final_reply = $1,
            approved_by = $2,
            approved_at = NOW()
        WHERE id = $3
        """,
        payload.final_reply,
        payload.approved_by,
        email_id,
    )

    background_tasks.add_task(
        send_auto_reply,
        recipient=row["sender"],
        subject=f"Re: {row['subject']}",
        body=payload.final_reply,
        email_id=email_id,
    )

    return {"status": "sent", "email_id": email_id}


# ---------------------------------------------------------------------------
# Endpoint: dashboard — alertas críticos do dia
# ---------------------------------------------------------------------------

@app.get("/api/v1/dashboard/alerts")
async def get_daily_alerts(db: asyncpg.Connection = Depends(get_db)):
    """Retorna alertas críticos para o painel matinal do gestor."""
    today = date.today()

    # Payouts solicitados hoje
    payouts = await db.fetch(
        """
        SELECT t.id as trader_id, t.name, pr.amount, pr.requested_at
        FROM payout_requests pr
        JOIN traders t ON t.id = pr.trader_id
        WHERE pr.requested_at::date = $1 AND pr.status = 'pending'
        ORDER BY pr.requested_at DESC
        """,
        today,
    )

    # Violações de DD nas últimas 24h
    violations = await db.fetch(
        """
        SELECT t.id as trader_id, t.name, t.phase, v.violation_type,
               v.amount_breached, v.occurred_at
        FROM dd_violations v
        JOIN traders t ON t.id = v.trader_id
        WHERE v.occurred_at >= NOW() - INTERVAL '24 hours'
        ORDER BY v.occurred_at DESC
        """,
    )

    # Contratos expirando em 48h
    expiring = await db.fetch(
        """
        SELECT t.id as trader_id, t.name, t.phase, c.expires_at,
               c.profit_current_pct, c.profit_target_pct
        FROM contracts c
        JOIN traders t ON t.id = c.trader_id
        WHERE c.expires_at BETWEEN NOW() AND NOW() + INTERVAL '48 hours'
          AND c.status = 'active'
        ORDER BY c.expires_at ASC
        """,
    )

    # KYC pendentes de auditoria
    kyc_pending = await db.fetch(
        "SELECT id, name, kyc_submitted_at FROM traders WHERE kyc_status = 'pending_review'"
    )

    return {
        "date": today.isoformat(),
        "payouts_today": [dict(r) for r in payouts],
        "dd_violations_24h": [dict(r) for r in violations],
        "expiring_contracts_48h": [dict(r) for r in expiring],
        "kyc_pending_review": [dict(r) for r in kyc_pending],
        "summary": {
            "total_critical": len(violations) + len(payouts),
            "total_high": len(expiring),
            "kyc_queue": len(kyc_pending),
        },
    }


# ---------------------------------------------------------------------------
# Endpoint: agenda semanal com prioridade de mercado
# ---------------------------------------------------------------------------

@app.get("/api/v1/tasks/week")
async def get_weekly_tasks(db: asyncpg.Connection = Depends(get_db)):
    """Retorna tarefas da semana com ordenação por calendário de mercado."""
    tasks = await db.fetch(
        """
        SELECT t.*, tr.name as trader_name, me.event_name, me.impact_level
        FROM tasks t
        LEFT JOIN traders tr ON tr.id = t.trader_id
        LEFT JOIN market_events me ON me.event_date = t.due_date
        WHERE t.due_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 6
          AND t.status != 'cancelled'
        ORDER BY
          CASE WHEN me.impact_level = 'high' THEN 0 ELSE 1 END,  -- payroll/fomc first
          CASE t.priority
            WHEN 'critical' THEN 0
            WHEN 'high'     THEN 1
            WHEN 'medium'   THEN 2
            WHEN 'low'      THEN 3
          END,
          t.due_date,
          t.created_at
        """,
    )
    return [dict(r) for r in tasks]


# ---------------------------------------------------------------------------
# Endpoint: inbox — fila de e-mails para revisão do gestor
# ---------------------------------------------------------------------------

@app.get("/api/v1/emails/queue")
async def get_email_queue(db: asyncpg.Connection = Depends(get_db)):
    """E-mails pendentes de aprovação humana, ordenados por urgência."""
    rows = await db.fetch(
        """
        SELECT e.id, e.sender, e.subject, e.received_at,
               e.category, e.urgency, e.confidence_score,
               e.draft_reply, e.ai_reasoning,
               t.name as trader_name, t.phase as trader_phase
        FROM emails e
        LEFT JOIN traders t ON t.id = e.trader_id
        WHERE e.status IN ('pending_review', 'auto_queued')
        ORDER BY
          CASE e.urgency
            WHEN 'critical' THEN 0
            WHEN 'high'     THEN 1
            WHEN 'medium'   THEN 2
            WHEN 'low'      THEN 3
          END,
          e.received_at DESC
        LIMIT 50
        """,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helper: envio de e-mail (Gmail API / SMTP)
# ---------------------------------------------------------------------------

async def send_auto_reply(recipient: str, subject: str, body: str, email_id: int):
    """
    Integração com Gmail API (OAuth2) ou SMTP.
    Em produção: usar google-auth + googleapiclient.
    """
    import smtplib
    from email.mime.text import MIMEText

    # Variáveis de ambiente para SMTP/Gmail
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    from_addr = os.getenv("FROM_ADDRESS", smtp_user)

    if not smtp_user:
        print(f"[MOCK] Enviaria e-mail para {recipient}: {subject}")
        return

    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = recipient

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)

    # Marcar como enviado no banco
    conn = await asyncpg.connect(DB_URL)
    await conn.execute(
        "UPDATE emails SET status = 'sent', sent_at = NOW() WHERE id = $1",
        email_id,
    )
    await conn.close()


# ---------------------------------------------------------------------------
# Inicialização
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend_email_ai:app", host="0.0.0.0", port=8000, reload=True)
