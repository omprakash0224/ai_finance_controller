import React, { useState } from 'react';
import { AlertOctagon, CheckCircle2, ShieldAlert, ArrowRight, UserCheck, HelpCircle } from 'lucide-react';

export default function ExceptionList({ exceptions = [] }) {
  const [resolvedIds, setResolvedIds] = useState(new Set());
  const [escalatedIds, setEscalatedIds] = useState(new Set());

  const handleResolve = (id) => {
    const next = new Set(resolvedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setResolvedIds(next);
  };

  const handleEscalate = (id) => {
    const next = new Set(escalatedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setEscalatedIds(next);
  };

  return (
    <div className="panel-container">
      <div className="panel-header">
        <div>
          <h3 className="panel-title">
            <ShieldAlert size={20} style={{ color: 'var(--color-ruby)' }} />
            Unreconciled Exceptions ({exceptions.length})
          </h3>
          <p style={{ fontSize: '13px', color: 'var(--color-ink-mute)', marginTop: '2px' }}>
            Every exception includes an auditable reasoning chain and an actionable human resolution path.
          </p>
        </div>

        <div style={{ display: 'flex', gap: '8px' }}>
          <span className="pill-tag pill-tag-ruby">
            {exceptions.length - resolvedIds.size} Pending Review
          </span>
          {resolvedIds.size > 0 && (
            <span className="pill-tag pill-tag-success">
              {resolvedIds.size} Human Resolved
            </span>
          )}
        </div>
      </div>

      {exceptions.length === 0 ? (
        <div className="card-feature-light" style={{ textAlign: 'center', padding: '48px 24px' }}>
          <CheckCircle2 size={36} style={{ color: 'var(--color-emerald)', margin: '0 auto 12px' }} />
          <h4 style={{ fontSize: '18px', fontWeight: 500, color: 'var(--color-ink)' }}>No Outstanding Exceptions</h4>
          <p style={{ fontSize: '14px', color: 'var(--color-ink-mute)', marginTop: '4px' }}>
            All 60 payments in the synthetic batch have been matched cleanly or pipeline has not been executed yet.
          </p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {exceptions.map((item, index) => {
            const id = item.exception_id || index;
            const isResolved = resolvedIds.has(id);
            const isEscalated = escalatedIds.has(id);

            return (
              <div
                key={id}
                className="card-feature-light"
                style={{
                  borderLeft: isResolved
                    ? '4px solid var(--color-emerald)'
                    : isEscalated
                    ? '4px solid var(--color-purple)'
                    : '4px solid var(--color-ruby)',
                  background: isResolved ? 'rgba(5, 150, 105, 0.02)' : 'var(--color-canvas)',
                  opacity: isResolved ? 0.75 : 1,
                  transition: 'all 0.2s ease'
                }}
              >
                {/* Exception Header Row */}
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <span style={{
                      fontFamily: 'monospace',
                      fontWeight: 600,
                      fontSize: '13px',
                      color: 'var(--color-ink-secondary)',
                      background: 'var(--color-canvas-soft)',
                      padding: '3px 8px',
                      borderRadius: '4px'
                    }}>
                      {item.record_id || item.pay_id}
                    </span>
                    {item.settlement_id && (
                      <span style={{ fontSize: '12px', color: 'var(--color-ink-mute)', fontFamily: 'monospace' }}>
                        Ref: {item.settlement_id}
                      </span>
                    )}
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                    <span style={{ fontSize: '16px', fontWeight: 600, color: 'var(--color-ink)' }} className="tnum">
                      ₹{Number(item.net_amount || item.amount || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                    </span>
                    {isResolved ? (
                      <span className="pill-tag pill-tag-success"><CheckCircle2 size={12} /> Approved</span>
                    ) : isEscalated ? (
                      <span className="pill-tag pill-tag-purple"><UserCheck size={12} /> Escalated</span>
                    ) : (
                      <span className="pill-tag pill-tag-ruby"><AlertOctagon size={12} /> Exception</span>
                    )}
                  </div>
                </div>

                {/* Reason */}
                <div style={{ marginBottom: '10px' }}>
                  <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--color-ruby)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>
                    Failure Reason:
                  </span>
                  <p style={{ fontSize: '14px', color: 'var(--color-ink)', marginTop: '2px', fontWeight: 400 }}>
                    {item.reason}
                  </p>
                </div>

                {/* Agent Reasoning */}
                <div style={{
                  background: 'var(--color-canvas-soft)',
                  border: '1px solid var(--color-hairline)',
                  borderRadius: 'var(--rounded-md)',
                  padding: '12px 14px',
                  marginBottom: '12px',
                  fontSize: '13px',
                  color: 'var(--color-ink-secondary)'
                }}>
                  <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--color-primary-deep)', marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                    <HelpCircle size={12} />
                    Agent Audit Log:
                  </div>
                  <p style={{ lineHeight: '1.5', fontFamily: 'system-ui, sans-serif' }}>
                    {item.agent_reasoning || 'No agent reasoning captured.'}
                  </p>
                </div>

                {/* Action Footer */}
                <div style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  paddingTop: '8px',
                  borderTop: '1px solid var(--color-hairline)'
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', color: 'var(--color-ink-mute)' }}>
                    <ArrowRight size={14} style={{ color: 'var(--color-primary)' }} />
                    <span style={{ fontWeight: 500 }}>Suggested Action:</span>
                    <span style={{ color: 'var(--color-ink)' }}>{item.suggested_action}</span>
                  </div>

                  <div style={{ display: 'flex', gap: '8px' }}>
                    <button
                      onClick={() => handleEscalate(id)}
                      className="button-secondary"
                      style={{ fontSize: '12px', padding: '5px 12px' }}
                    >
                      {isEscalated ? 'De-escalate' : 'Escalate to Ops'}
                    </button>

                    <button
                      onClick={() => handleResolve(id)}
                      className="button-primary-pill"
                      style={{ fontSize: '12px', padding: '5px 14px' }}
                    >
                      {isResolved ? 'Re-open Exception' : 'Approve Action'}
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
