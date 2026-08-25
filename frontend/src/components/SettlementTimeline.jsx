import React, { useState } from 'react';
import { Calendar, Clock, CheckCircle2, AlertCircle, ArrowUpRight, Zap, Filter } from 'lucide-react';

export default function SettlementTimeline({ settlements = [] }) {
  const [filterTier, setFilterTier] = useState('ALL');

  const filteredSettlements = settlements.filter((s) => {
    if (filterTier === 'PENDING') return s.status === 'pending';
    if (filterTier === 'PROCESSED') return s.status === 'processed';
    if (filterTier === 'T0') return s.settlement_tier === 'T0';
    if (filterTier === 'T1') return s.settlement_tier === 'T1';
    if (filterTier === 'T2') return s.settlement_tier === 'T2';
    return true;
  });

  const totalPendingAmount = settlements
    .filter((s) => s.status === 'pending')
    .reduce((sum, s) => sum + Number(s.total_amount || 0), 0);

  const totalProcessedAmount = settlements
    .filter((s) => s.status === 'processed')
    .reduce((sum, s) => sum + Number(s.total_amount || 0), 0);

  return (
    <div className="panel-container">
      {/* Header & Filter Controls */}
      <div className="panel-header">
        <div>
          <h3 className="panel-title">
            <Clock size={20} style={{ color: 'var(--color-primary)' }} />
            Razorpay Settlement Timeline (T+0 / T+1 / T+2 Cycles)
          </h3>
          <p style={{ fontSize: '13px', color: 'var(--color-ink-mute)', marginTop: '2px' }}>
            Tracks captured payment batches through Indian banking settlement windows.
          </p>
        </div>

        <div className="filter-pills">
          <button
            className={`filter-pill-btn ${filterTier === 'ALL' ? 'active' : ''}`}
            onClick={() => setFilterTier('ALL')}
          >
            All ({settlements.length})
          </button>
          <button
            className={`filter-pill-btn ${filterTier === 'PENDING' ? 'active' : ''}`}
            onClick={() => setFilterTier('PENDING')}
          >
            Pending Inflows
          </button>
          <button
            className={`filter-pill-btn ${filterTier === 'PROCESSED' ? 'active' : ''}`}
            onClick={() => setFilterTier('PROCESSED')}
          >
            Bank Cleared
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: '16px' }}>
        <div className="card-feature-light" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <span style={{ fontSize: '12px', color: 'var(--color-ink-mute)', textTransform: 'uppercase', letterSpacing: '0.4px', fontWeight: 600 }}>
              Bank Cleared Volume
            </span>
            <div style={{ fontSize: '22px', fontWeight: 600, color: 'var(--color-emerald)', marginTop: '4px' }} className="tnum">
              ₹{totalProcessedAmount.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
            </div>
            <span style={{ fontSize: '12px', color: 'var(--color-ink-mute)' }}>
              {settlements.filter((s) => s.status === 'processed').length} Batch Settlements
            </span>
          </div>
          <CheckCircle2 size={28} style={{ color: 'var(--color-emerald)', opacity: 0.8 }} />
        </div>

        <div className="card-cream-band" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <span style={{ fontSize: '12px', color: 'var(--color-lemon)', textTransform: 'uppercase', letterSpacing: '0.4px', fontWeight: 600 }}>
              Pending Cash Inflows
            </span>
            <div style={{ fontSize: '22px', fontWeight: 600, color: 'var(--color-ink)', marginTop: '4px' }} className="tnum">
              ₹{totalPendingAmount.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
            </div>
            <span style={{ fontSize: '12px', color: 'var(--color-ink-mute)' }}>
              T+1 & T+2 Bank Pipeline
            </span>
          </div>
          <Clock size={28} style={{ color: 'var(--color-amber)', opacity: 0.8 }} />
        </div>
      </div>

      {/* Settlements Table / Timeline Grid */}
      <div className="data-table-wrapper">
        <table className="data-table">
          <thead>
            <tr>
              <th>Settlement ID</th>
              <th>Settlement Cycle</th>
              <th>Settlement Date</th>
              <th>Bank Reference (UTR)</th>
              <th style={{ textAlign: 'right' }}>Total Volume (INR)</th>
              <th style={{ textAlign: 'center' }}>Payments Count</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {filteredSettlements.length === 0 ? (
              <tr>
                <td colSpan="7" style={{ textAlign: 'center', padding: '32px', color: 'var(--color-ink-mute)' }}>
                  No settlement records found.
                </td>
              </tr>
            ) : (
              filteredSettlements.map((item, idx) => (
                <tr key={idx}>
                  <td>
                    <span style={{ fontFamily: 'monospace', fontWeight: 600, fontSize: '13px', color: 'var(--color-primary-deep)' }}>
                      {item.settlement_id}
                    </span>
                  </td>
                  <td>
                    {item.settlement_tier === 'T0' ? (
                      <span className="pill-tag pill-tag-purple"><Zap size={11} /> T+0 Instant</span>
                    ) : item.settlement_tier === 'T2' ? (
                      <span className="pill-tag pill-tag-amber">T+2 Standard</span>
                    ) : (
                      <span className="pill-tag pill-tag-soft">T+1 Standard</span>
                    )}
                  </td>
                  <td>
                    <span className="tnum" style={{ fontSize: '13px', color: 'var(--color-ink-secondary)' }}>
                      {item.settlement_date}
                    </span>
                  </td>
                  <td>
                    <span className="tnum" style={{ fontFamily: 'monospace', fontSize: '12px', color: 'var(--color-purple)' }}>
                      {item.settlement_utr || 'Pending Generation'}
                    </span>
                  </td>
                  <td style={{ textAlign: 'right', fontWeight: 600 }} className="tnum">
                    ₹{Number(item.total_amount || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                  </td>
                  <td style={{ textAlign: 'center' }} className="tnum">
                    {item.num_payments || 1}
                  </td>
                  <td>
                    {item.status === 'processed' ? (
                      <span className="pill-tag pill-tag-success"><CheckCircle2 size={12} /> Bank Credited</span>
                    ) : (
                      <span className="pill-tag pill-tag-amber"><Clock size={12} /> Pending Inflow</span>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
