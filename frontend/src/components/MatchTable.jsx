import React, { useState, useMemo } from 'react';
import { Search, ShieldCheck, AlertCircle, ArrowUpDown, Filter, CheckCircle } from 'lucide-react';

export default function MatchTable({ matchResults = [] }) {
  const [searchTerm, setSearchTerm] = useState('');
  const [filterType, setFilterType] = useState('ALL');
  const [sortField, setSortField] = useState('pay_id');
  const [sortOrder, setSortOrder] = useState('asc');

  const handleSort = (field) => {
    if (sortField === field) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortOrder('asc');
    }
  };

  const filteredData = useMemo(() => {
    return matchResults.filter((row) => {
      // Filter by tab pill
      if (filterType === 'MATCHED' && row.status !== 'matched') return false;
      if (filterType === 'EXCEPTION' && row.status !== 'exception') return false;
      if (filterType === 'UTR' && row.match_type !== 'utr_match') return false;
      if (filterType === 'FUZZY' && !['fuzzy_amount', 'fuzzy_date'].includes(row.match_type)) return false;

      // Filter by search query
      if (!searchTerm) return true;
      const query = searchTerm.toLowerCase();
      return (
        (row.pay_id && row.pay_id.toLowerCase().includes(query)) ||
        (row.settlement_id && row.settlement_id.toLowerCase().includes(query)) ||
        (row.entry_id && row.entry_id.toLowerCase().includes(query)) ||
        (row.settlement_utr && row.settlement_utr.toLowerCase().includes(query)) ||
        (row.match_type && row.match_type.toLowerCase().includes(query))
      );
    }).sort((a, b) => {
      let valA = a[sortField] ?? '';
      let valB = b[sortField] ?? '';

      if (typeof valA === 'number' && typeof valB === 'number') {
        return sortOrder === 'asc' ? valA - valB : valB - valA;
      }
      valA = String(valA).toLowerCase();
      valB = String(valB).toLowerCase();
      if (valA < valB) return sortOrder === 'asc' ? -1 : 1;
      if (valA > valB) return sortOrder === 'asc' ? 1 : -1;
      return 0;
    });
  }, [matchResults, searchTerm, filterType, sortField, sortOrder]);

  const renderMatchTypeBadge = (type) => {
    switch (type) {
      case 'utr_match':
        return <span className="pill-tag pill-tag-purple">UTR Cross-Ref</span>;
      case 'exact':
        return <span className="pill-tag pill-tag-success">Exact Match</span>;
      case 'fuzzy_amount':
        return <span className="pill-tag pill-tag-amber">Fuzzy Amount (Δ≤5)</span>;
      case 'fuzzy_date':
        return <span className="pill-tag pill-tag-amber">Fuzzy Date (Δ≤2d)</span>;
      case 'split_match':
        return <span className="pill-tag pill-tag-purple">1-to-N Split</span>;
      case 'unmatched':
      default:
        return <span className="pill-tag pill-tag-ruby">Unmatched</span>;
    }
  };

  const renderStatusBadge = (status) => {
    if (status === 'matched') {
      return <span className="pill-tag pill-tag-success"><CheckCircle size={12} /> Matched</span>;
    }
    return <span className="pill-tag pill-tag-ruby"><AlertCircle size={12} /> Exception</span>;
  };

  return (
    <div className="data-table-wrapper">
      {/* Header controls & Filters */}
      <div className="table-filter-bar">
        <div className="table-search-box">
          <Search size={14} className="text-slate-400" />
          <input
            type="text"
            placeholder="Search pay_id, setl_id, UTR, entry..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
          />
        </div>

        <div className="filter-pills">
          <button
            className={`filter-pill-btn ${filterType === 'ALL' ? 'active' : ''}`}
            onClick={() => setFilterType('ALL')}
          >
            All ({matchResults.length})
          </button>
          <button
            className={`filter-pill-btn ${filterType === 'MATCHED' ? 'active' : ''}`}
            onClick={() => setFilterType('MATCHED')}
          >
            Matched ({matchResults.filter((r) => r.status === 'matched').length})
          </button>
          <button
            className={`filter-pill-btn ${filterType === 'EXCEPTION' ? 'active' : ''}`}
            onClick={() => setFilterType('EXCEPTION')}
          >
            Exceptions ({matchResults.filter((r) => r.status === 'exception').length})
          </button>
          <button
            className={`filter-pill-btn ${filterType === 'UTR' ? 'active' : ''}`}
            onClick={() => setFilterType('UTR')}
          >
            UTR Fast-Path ({matchResults.filter((r) => r.match_type === 'utr_match').length})
          </button>
          <button
            className={`filter-pill-btn ${filterType === 'FUZZY' ? 'active' : ''}`}
            onClick={() => setFilterType('FUZZY')}
          >
            Fuzzy ({matchResults.filter((r) => ['fuzzy_amount', 'fuzzy_date'].includes(r.match_type)).length})
          </button>
        </div>
      </div>

      {/* Main Table */}
      <div style={{ overflowX: 'auto' }}>
        <table className="data-table">
          <thead>
            <tr>
              <th onClick={() => handleSort('pay_id')} style={{ cursor: 'pointer' }}>
                Payment ID <ArrowUpDown size={12} style={{ display: 'inline', marginLeft: '4px' }} />
              </th>
              <th onClick={() => handleSort('settlement_id')} style={{ cursor: 'pointer' }}>
                Settlement ID
              </th>
              <th onClick={() => handleSort('entry_id')} style={{ cursor: 'pointer' }}>
                Ledger Entry
              </th>
              <th onClick={() => handleSort('net_amount')} style={{ cursor: 'pointer', textAlign: 'right' }}>
                Net Amount (INR) <ArrowUpDown size={12} style={{ display: 'inline', marginLeft: '4px' }} />
              </th>
              <th>Match Type</th>
              <th onClick={() => handleSort('confidence')} style={{ cursor: 'pointer', textAlign: 'center' }}>
                Confidence
              </th>
              <th>Status</th>
              <th>Ground Truth Label</th>
            </tr>
          </thead>
          <tbody>
            {filteredData.length === 0 ? (
              <tr>
                <td colSpan="8" style={{ textAlign: 'center', padding: '32px', color: 'var(--color-ink-mute)' }}>
                  No match results found matching your search.
                </td>
              </tr>
            ) : (
              filteredData.map((row, idx) => (
                <tr key={idx}>
                  <td>
                    <div style={{ fontWeight: 500, fontFamily: 'monospace', fontSize: '13px' }}>
                      {row.pay_id}
                    </div>
                    {row.method && (
                      <div style={{ fontSize: '11px', color: 'var(--color-ink-mute)' }}>
                        Method: {row.method.toUpperCase()}
                      </div>
                    )}
                  </td>
                  <td>
                    <div style={{ fontFamily: 'monospace', fontSize: '12px', color: 'var(--color-ink-secondary)' }}>
                      {row.settlement_id || '—'}
                    </div>
                    {row.settlement_utr && (
                      <div style={{ fontSize: '11px', color: 'var(--color-purple)' }} className="tnum">
                        UTR: {row.settlement_utr}
                      </div>
                    )}
                  </td>
                  <td>
                    <span style={{ fontFamily: 'monospace', fontSize: '12px' }}>
                      {row.entry_id || <span style={{ color: '#94a3b8' }}>Unlinked</span>}
                    </span>
                  </td>
                  <td style={{ textAlign: 'right', fontWeight: 600 }} className="tnum">
                    ₹{Number(row.net_amount || row.amount || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                  </td>
                  <td>{renderMatchTypeBadge(row.match_type)}</td>
                  <td style={{ textAlign: 'center', fontWeight: 500 }} className="tnum">
                    {Math.round((row.confidence || 0) * 100)}%
                  </td>
                  <td>{renderStatusBadge(row.status)}</td>
                  <td>
                    <span
                      style={{
                        fontSize: '11px',
                        padding: '2px 8px',
                        borderRadius: '4px',
                        background: 'var(--color-canvas-soft)',
                        border: '1px solid var(--color-hairline)',
                        color: 'var(--color-ink-mute)',
                        fontFamily: 'monospace'
                      }}
                    >
                      {row.ground_truth_error_type || 'clean'}
                    </span>
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
