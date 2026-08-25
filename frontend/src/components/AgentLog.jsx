import React, { useEffect, useRef, useState } from 'react';
import { Play, Terminal, CheckCircle2, AlertTriangle, ArrowRight, ShieldCheck, Sparkles, RefreshCw } from 'lucide-react';

export default function AgentLog({ logs = [], isStreaming = false, progress = 0, onRunPipeline }) {
  const logContainerRef = useRef(null);
  const [autoScroll, setAutoScroll] = useState(true);

  useEffect(() => {
    if (autoScroll && logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [logs, autoScroll]);

  const renderEventIcon = (type) => {
    switch (type) {
      case 'tool_call':
        return <Terminal size={14} className="text-indigo-400" />;
      case 'observation':
        return <ArrowRight size={14} className="text-emerald-400" />;
      case 'thought':
        return <Sparkles size={14} className="text-amber-400" />;
      case 'step_complete':
        return <CheckCircle2 size={14} className="text-emerald-400" />;
      case 'report_ready':
        return <ShieldCheck size={14} className="text-purple-400" />;
      case 'error':
        return <AlertTriangle size={14} className="text-rose-400" />;
      default:
        return <Terminal size={14} className="text-slate-400" />;
    }
  };

  return (
    <div className="card-dashboard-mockup flex flex-col h-[520px] max-h-[520px]">
      {/* Chrome Header */}
      <div style={{
        padding: '12px 18px',
        background: 'rgba(0, 0, 0, 0.3)',
        borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between'
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <div style={{ display: 'flex', gap: '6px' }}>
            <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: '#ff5f56' }} />
            <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: '#ffbd2e' }} />
            <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: '#27c93f' }} />
          </div>
          <span style={{ fontSize: '13px', fontWeight: 500, color: '#94a3b8', letterSpacing: '0.2px' }}>
            agent-orchestrator :: live-stream (ADK 2.7.0)
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          {isStreaming ? (
            <span style={{ fontSize: '12px', color: '#38bdf8', display: 'flex', alignItems: 'center', gap: '6px' }}>
              <RefreshCw size={12} className="animate-spin" />
              ReAct Agent Active...
            </span>
          ) : (
            <button
              onClick={onRunPipeline}
              className="button-primary-pill"
              style={{ padding: '5px 12px', fontSize: '12px' }}
            >
              <Play size={12} />
              Run Pipeline
            </button>
          )}

          <label style={{ fontSize: '12px', color: '#64748b', display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              style={{ accentColor: '#533afd' }}
            />
            Auto-scroll
          </label>
        </div>
      </div>

      {/* Progress Bar when running */}
      {isStreaming && (
        <div style={{ width: '100%', height: '3px', background: 'rgba(255,255,255,0.05)' }}>
          <div
            style={{
              height: '100%',
              width: `${Math.min(progress, 100)}%`,
              background: 'linear-gradient(90deg, #533afd, #ea2261)',
              transition: 'width 0.3s ease'
            }}
          />
        </div>
      )}

      {/* Log Terminal Stream */}
      <div
        ref={logContainerRef}
        className="custom-scrollbar"
        style={{
          padding: '16px 20px',
          overflowY: 'auto',
          flex: 1,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
          fontSize: '13px',
          lineHeight: 1.6,
          background: '#0c0f24'
        }}
      >
        {logs.length === 0 ? (
          <div style={{ color: '#64748d', padding: '40px 0', textAlign: 'center' }}>
            <p style={{ marginBottom: '8px' }}>Pipeline ready. Click "Run Reconciliation Pipeline" to start.</p>
            <p style={{ fontSize: '12px', opacity: 0.7 }}>
              Agent will process 60 payments against bank records & ledger entries in real time.
            </p>
          </div>
        ) : (
          logs.map((log, index) => {
            const timestamp = log.time || new Date().toLocaleTimeString();
            return (
              <div
                key={index}
                style={{
                  marginBottom: '10px',
                  paddingBottom: '8px',
                  borderBottom: '1px solid rgba(255, 255, 255, 0.04)',
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: '10px'
                }}
              >
                <span style={{ color: '#475569', fontSize: '11px', whiteSpace: 'nowrap', paddingTop: '2px' }}>
                  [{timestamp}]
                </span>
                <div style={{ display: 'flex', alignItems: 'center', paddingTop: '3px' }}>
                  {renderEventIcon(log.type)}
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{
                      fontWeight: 600,
                      fontSize: '12px',
                      color: log.type === 'tool_call' ? '#818cf8' : log.type === 'observation' ? '#34d399' : log.type === 'thought' ? '#fbbf24' : '#e2e8f0'
                    }}>
                      {log.agent ? `[${log.agent}]` : log.type.toUpperCase()}
                    </span>
                    {log.message && <span style={{ color: '#cbd5e1' }}>{log.message}</span>}
                  </div>

                  {/* Render Tool Name & Parameters */}
                  {log.tool && (
                    <div style={{
                      marginTop: '4px',
                      background: 'rgba(83, 58, 253, 0.12)',
                      border: '1px solid rgba(83, 58, 253, 0.25)',
                      borderRadius: '4px',
                      padding: '4px 8px',
                      color: '#a5b4fc',
                      fontSize: '12px'
                    }}>
                      <code>call {log.tool}({JSON.stringify(log.args || {})})</code>
                    </div>
                  )}

                  {/* Render Detailed Output / Summary if present */}
                  {log.output && (
                    <div style={{
                      marginTop: '4px',
                      background: 'rgba(0, 0, 0, 0.25)',
                      borderRadius: '4px',
                      padding: '6px 10px',
                      color: '#94a3b8',
                      fontSize: '12px'
                    }}>
                      {typeof log.output === 'object' ? JSON.stringify(log.output, null, 2) : log.output}
                    </div>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
