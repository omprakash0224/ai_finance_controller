import React, { useState, useEffect, useRef } from 'react';
import {
  MessageSquare,
  Send,
  Sparkles,
  Database,
  Terminal,
  Loader2,
  CheckCircle2,
  Clock,
  Cpu,
  Bot
} from 'lucide-react';

const SUGGESTED_QUESTIONS = [
  "How much is currently pending settlement?",
  "Which payments failed matching and why?",
  "Show me all T+2 settlement records",
  "What is total GST collected on UPI payments?",
  "List all UTR matched bank credits"
];

const LOADING_STEPS = [
  { id: 'parse', label: "Parsing query semantics & identifying financial intent...", icon: Cpu },
  { id: 'sql', label: "Mapping tables & constructing Neon PostgreSQL AST query...", icon: Terminal },
  { id: 'db', label: "Executing SQL query against Neon database...", icon: Database },
  { id: 'format', label: "Synthesizing tabular answer & analytics report...", icon: Sparkles }
];

export default function SettlementQA() {
  const [messages, setMessages] = useState([
    {
      sender: 'agent',
      text: 'Hello! I am your Settlement Q&A Agent. Ask me any question in natural language about Razorpay payments, bank statement credits, ledger entries, or tax line tagging.',
      sql: null,
      tableData: null
    }
  ]);
  const [inputQuery, setInputQuery] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [activeStepIdx, setActiveStepIdx] = useState(0);
  const [elapsedMs, setElapsedMs] = useState(0);

  const messagesEndRef = useRef(null);
  const timerRef = useRef(null);
  const stepTimerRef = useRef(null);

  // Auto-scroll chat box on new messages or loading updates
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, isLoading, activeStepIdx]);

  // Handle active loading timers & step progression
  useEffect(() => {
    if (isLoading) {
      setElapsedMs(0);
      setActiveStepIdx(0);

      // Start high-precision elapsed timer
      const startTime = Date.now();
      timerRef.current = setInterval(() => {
        setElapsedMs(Date.now() - startTime);
      }, 100);

      // Progressively advance through realistic reasoning steps while waiting
      stepTimerRef.current = setInterval(() => {
        setActiveStepIdx((prev) => (prev < LOADING_STEPS.length - 1 ? prev + 1 : prev));
      }, 1100);
    } else {
      if (timerRef.current) clearInterval(timerRef.current);
      if (stepTimerRef.current) clearInterval(stepTimerRef.current);
    }

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      if (stepTimerRef.current) clearInterval(stepTimerRef.current);
    };
  }, [isLoading]);

  const handleSend = async (questionText) => {
    const q = questionText || inputQuery;
    if (!q.trim() || isLoading) return;

    // Add user message
    const userMsg = { sender: 'user', text: q };
    setMessages((prev) => [...prev, userMsg]);
    setInputQuery('');
    setIsLoading(true);

    try {
      const response = await fetch('/api/qa', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q })
      });

      if (!response.ok) {
        throw new Error(`Server returned HTTP ${response.status}`);
      }

      const data = await response.json();

      const agentMsg = {
        sender: 'agent',
        text: data.answer || data.text || 'Query processed successfully.',
        sql: data.sql_query || data.sql || null,
        tableData: data.rows || data.data || (Array.isArray(data.result) ? data.result : null)
      };

      setMessages((prev) => [...prev, agentMsg]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          sender: 'agent',
          text: `Error processing query: ${err.message}. Please verify backend server is running.`,
          isError: true
        }
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="panel-container">
      {/* Header */}
      <div className="panel-header">
        <div>
          <h3 className="panel-title">
            <MessageSquare size={20} style={{ color: 'var(--color-primary)' }} />
            Natural-Language Settlement Q&A Agent
          </h3>
          <p style={{ fontSize: '13px', color: 'var(--color-ink-mute)', marginTop: '2px' }}>
            Translates financial questions to Neon PostgreSQL queries and returns structured tabular answers.
          </p>
        </div>

        <span className="pill-tag pill-tag-soft">
          <Database size={12} /> NL-to-SQL ADK Agent
        </span>
      </div>

      {/* Suggested Questions Chips */}
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '8px' }}>
        {SUGGESTED_QUESTIONS.map((q, idx) => (
          <button
            key={idx}
            onClick={() => handleSend(q)}
            className="button-secondary"
            style={{ fontSize: '12px', padding: '4px 12px' }}
            disabled={isLoading}
          >
            <Sparkles size={12} style={{ color: 'var(--color-primary)' }} />
            {q}
          </button>
        ))}
      </div>

      {/* Chat Messages Container */}
      <div
        className="card-feature-light custom-scrollbar"
        style={{
          height: '420px',
          overflowY: 'auto',
          display: 'flex',
          flexDirection: 'column',
          gap: '16px',
          padding: '20px'
        }}
      >
        {messages.map((msg, index) => (
          <div
            key={index}
            style={{
              alignSelf: msg.sender === 'user' ? 'flex-end' : 'flex-start',
              maxWidth: msg.sender === 'user' ? '80%' : '90%',
              display: 'flex',
              flexDirection: 'column',
              alignItems: msg.sender === 'user' ? 'flex-end' : 'flex-start'
            }}
          >
            {/* Sender Label */}
            <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--color-ink-mute)', marginBottom: '4px' }}>
              {msg.sender === 'user' ? 'You' : 'Settlement Q&A Agent'}
            </span>

            {/* Bubble */}
            <div
              style={{
                background: msg.sender === 'user' ? 'var(--color-primary)' : 'var(--color-canvas-soft)',
                color: msg.sender === 'user' ? '#ffffff' : 'var(--color-ink)',
                border: msg.sender === 'user' ? 'none' : '1px solid var(--color-hairline)',
                borderRadius: msg.sender === 'user' ? '16px 16px 4px 16px' : '16px 16px 16px 4px',
                padding: '12px 16px',
                boxShadow: msg.sender === 'user' ? '0 2px 8px rgba(83, 58, 253, 0.25)' : 'var(--shadow-level-1)',
                fontSize: '14px',
                lineHeight: '1.5'
              }}
            >
              <p style={{ color: msg.isError ? 'var(--color-ruby)' : 'inherit' }}>{msg.text}</p>

              {/* Render SQL Block if present */}
              {msg.sql && (
                <div style={{ marginTop: '10px', background: '#0b0c2a', borderRadius: '6px', padding: '10px 12px', border: '1px solid rgba(255,255,255,0.1)' }}>
                  <div style={{ fontSize: '11px', color: '#818cf8', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
                    <Terminal size={12} /> Generated PostgreSQL Query:
                  </div>
                  <code style={{ fontSize: '12px', color: '#38bdf8', fontFamily: 'monospace', display: 'block', whiteSpace: 'pre-wrap' }}>
                    {msg.sql}
                  </code>
                </div>
              )}

              {/* Render Structured Result Table if present */}
              {msg.tableData && Array.isArray(msg.tableData) && msg.tableData.length > 0 && (
                <div style={{ marginTop: '12px', overflowX: 'auto', background: 'var(--color-canvas)', borderRadius: '6px', border: '1px solid var(--color-hairline)' }}>
                  <table style={{ width: '100%', fontSize: '12px', borderCollapse: 'collapse', textAlign: 'left' }}>
                    <thead>
                      <tr style={{ background: 'var(--color-canvas-soft)', borderBottom: '1px solid var(--color-hairline)' }}>
                        {Object.keys(msg.tableData[0]).map((colKey, i) => (
                          <th key={i} style={{ padding: '6px 10px', color: 'var(--color-ink-mute)', fontWeight: 600 }}>
                            {colKey}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {msg.tableData.slice(0, 10).map((row, rIdx) => (
                        <tr key={rIdx} style={{ borderBottom: '1px solid var(--color-hairline)' }}>
                          {Object.values(row).map((val, cIdx) => (
                            <td key={cIdx} style={{ padding: '6px 10px', color: 'var(--color-ink)' }} className={typeof val === 'number' ? 'tnum' : ''}>
                              {val !== null ? String(val) : '—'}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {msg.tableData.length > 10 && (
                    <div style={{ padding: '6px 10px', fontSize: '11px', color: 'var(--color-ink-mute)', textAlign: 'center' }}>
                      Showing top 10 of {msg.tableData.length} query results.
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}

        {/* Interactive Multi-Step Agent Reasoning Card */}
        {isLoading && (
          <div
            className="card-agent-loading"
            style={{
              alignSelf: 'flex-start',
              width: '88%',
              background: 'linear-gradient(135deg, #ffffff 0%, #f6f9fc 100%)',
              border: '1px solid var(--color-primary-subdued)',
              borderRadius: '16px 16px 16px 4px',
              padding: '16px 20px',
              boxShadow: 'var(--shadow-level-2)',
              marginTop: '4px'
            }}
          >
            {/* Agent Header & Timer */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px', borderBottom: '1px solid var(--color-hairline)', paddingBottom: '10px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <div style={{ position: 'relative', display: 'flex' }}>
                  <Bot size={18} style={{ color: 'var(--color-primary)' }} />
                  <span
                    style={{
                      position: 'absolute',
                      top: '-2px',
                      right: '-2px',
                      width: '8px',
                      height: '8px',
                      borderRadius: '50%',
                      background: 'var(--color-emerald)',
                      boxShadow: '0 0 6px var(--color-emerald)'
                    }}
                  />
                </div>
                <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-ink)' }}>
                  Settlement Q&A Agent Reasoning...
                </span>
              </div>

              <div
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '4px',
                  fontSize: '11px',
                  fontWeight: 600,
                  color: 'var(--color-primary)',
                  background: 'var(--color-primary-subdued-bg)',
                  padding: '3px 10px',
                  borderRadius: 'var(--rounded-pill)'
                }}
              >
                <Clock size={12} className="animate-spin" />
                {(elapsedMs / 1000).toFixed(1)}s elapsed
              </div>
            </div>

            {/* Live Interactive Steps Checklist */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '14px' }}>
              {LOADING_STEPS.map((step, idx) => {
                const StepIcon = step.icon;
                const isDone = idx < activeStepIdx;
                const isActive = idx === activeStepIdx;

                return (
                  <div
                    key={step.id}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '10px',
                      fontSize: '12px',
                      color: isDone
                        ? 'var(--color-ink-mute)'
                        : isActive
                        ? 'var(--color-primary-deep)'
                        : '#94a3b8',
                      fontWeight: isActive ? 600 : 400,
                      transition: 'all 0.25s ease'
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: '18px' }}>
                      {isDone ? (
                        <CheckCircle2 size={16} style={{ color: 'var(--color-emerald)' }} />
                      ) : isActive ? (
                        <Loader2 size={16} className="animate-spin" style={{ color: 'var(--color-primary)' }} />
                      ) : (
                        <div
                          style={{
                            width: '8px',
                            height: '8px',
                            borderRadius: '50%',
                            background: 'var(--color-hairline)'
                          }}
                        />
                      )}
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <StepIcon size={13} style={{ color: isActive ? 'var(--color-primary)' : 'inherit', opacity: isDone ? 0.7 : 1 }} />
                      <span style={{ textDecoration: isDone ? 'none' : 'none' }}>
                        {step.label}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Dark SQL Skeleton Preview Shimmer */}
            <div
              style={{
                background: '#0b0c2a',
                borderRadius: '8px',
                padding: '10px 12px',
                border: '1px solid rgba(255,255,255,0.08)'
              }}
            >
              <div
                style={{
                  fontSize: '11px',
                  color: '#818cf8',
                  fontWeight: 600,
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  marginBottom: '8px'
                }}
              >
                <Terminal size={12} /> Live ADK Pipeline Stream
              </div>

              {/* Skeleton Shimmer Bars */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                <div
                  className="skeleton-shimmer"
                  style={{
                    height: '10px',
                    width: '75%',
                    background: 'linear-gradient(90deg, #1e1b4b 25%, #312e81 50%, #1e1b4b 75%)'
                  }}
                />
                <div
                  className="skeleton-shimmer"
                  style={{
                    height: '10px',
                    width: '50%',
                    background: 'linear-gradient(90deg, #1e1b4b 25%, #312e81 50%, #1e1b4b 75%)'
                  }}
                />
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input Box */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          handleSend();
        }}
        style={{ display: 'flex', gap: '10px' }}
      >
        <div style={{ position: 'relative', flex: 1 }}>
          <input
            type="text"
            className="text-input"
            placeholder="Ask anything (e.g. 'Show all UPI settlements', 'What is total volume?')..."
            value={inputQuery}
            onChange={(e) => setInputQuery(e.target.value)}
            disabled={isLoading}
            style={{ borderRadius: 'var(--rounded-pill)', paddingRight: '40px' }}
          />
        </div>
        <button
          type="submit"
          className="button-primary-pill"
          disabled={!inputQuery.trim() || isLoading}
        >
          {isLoading ? (
            <>
              <Loader2 size={14} className="animate-spin" />
              Thinking...
            </>
          ) : (
            <>
              <Send size={14} />
              Ask Agent
            </>
          )}
        </button>
      </form>
    </div>
  );
}

