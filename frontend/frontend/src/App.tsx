import { useState, useRef, useEffect } from "react";

const API_URL = "http://127.0.0.1:8000";

interface Message {
  role: "user" | "assistant";
  content: string;
  sources?: { pdf: string; slide_num: number }[];
}

export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState<"tutor" | "search">("tutor");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const sendMessage = async () => {
    if (!input.trim()) return;
    const question = input;
    setInput("");
    setMessages(prev => [...prev, { role: "user", content: question }]);
    setLoading(true);

    try {
      const endpoint = mode === "tutor" ? "/api/ask" : "/api/search";
      const body = mode === "tutor"
        ? { question }
        : { query: question, top_k: 3 };

      const res = await fetch(`${API_URL}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      const answer = mode === "tutor"
        ? data.answer
        : data.results?.map((r: any) => r.text).join("\n\n---\n\n");

      setMessages(prev => [...prev, {
        role: "assistant",
        content: answer,
        sources: mode === "tutor" ? data.sources : data.results?.map((r: any) => r.metadata),
      }]);
    } catch {
      setMessages(prev => [...prev, {
        role: "assistant",
        content: "⚠️ Cannot connect to backend. Make sure it's running on port 8000.",
      }]);
    }
    setLoading(false);
  };

  return (
    <div style={{
      background: "#0D0D0F",
      minHeight: "100vh",
      color: "#fff",
      fontFamily: "'Inter', -apple-system, sans-serif",
      display: "flex",
      flexDirection: "column",
    }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #222; border-radius: 2px; }
        .glow-btn:hover { box-shadow: 0 0 20px rgba(0, 200, 200, 0.3); }
        .msg-btn:hover { background: #1a1a1a !important; border-color: #00C8C8 !important; color: #00C8C8 !important; }
        .send-btn:hover { box-shadow: 0 0 24px rgba(0, 200, 200, 0.4); }
        @keyframes gradient {
          0% { background-position: 0% 50%; }
          50% { background-position: 100% 50%; }
          100% { background-position: 0% 50%; }
        }
        @keyframes pulse {
          0%, 100% { opacity: 0.4; }
          50% { opacity: 1; }
        }
      `}</style>

      {/* Header */}
      <div style={{
        padding: "14px 28px",
        borderBottom: "1px solid #1a1a1f",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        backdropFilter: "blur(10px)",
        position: "sticky",
        top: 0,
        zIndex: 10,
        background: "rgba(13,13,15,0.95)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{
            fontSize: 24,
            fontWeight: 800,
            background: "linear-gradient(135deg, #00C8C8, #0080FF, #00C8C8)",
            backgroundSize: "200% 200%",
            animation: "gradient 3s ease infinite",
            WebkitBackgroundClip: "text",
            WebkitTextFillColor: "transparent",
            letterSpacing: "-0.5px",
          }}>
            ⚡ ReplyX
          </span>
          <span style={{
            color: "#3a3a4a",
            fontSize: 13,
            fontWeight: 500,
          }}>
            TUM AI Campus Copilot
          </span>
        </div>

        <div style={{ display: "flex", gap: 6 }}>
          {[
            { key: "tutor", label: "🎓 Tutor", desc: "Socratic learning" },
            { key: "search", label: "🔍 Search", desc: "Find slides" },
          ].map(m => (
            <button key={m.key} className="glow-btn" onClick={() => setMode(m.key as any)} style={{
              padding: "7px 18px",
              borderRadius: 22,
              border: `1px solid ${mode === m.key ? "#00C8C8" : "#222"}`,
              cursor: "pointer",
              fontSize: 13,
              fontWeight: 600,
              background: mode === m.key
                ? "linear-gradient(135deg, rgba(0,200,200,0.15), rgba(0,128,255,0.1))"
                : "transparent",
              color: mode === m.key ? "#00C8C8" : "#555",
              transition: "all 0.2s",
            }}>
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {/* Messages */}
      <div style={{
        flex: 1,
        overflowY: "auto",
        padding: "32px 24px",
        display: "flex",
        flexDirection: "column",
        gap: 20,
        maxWidth: 760,
        width: "100%",
        margin: "0 auto",
      }}>
        {messages.length === 0 && (
          <div style={{ textAlign: "center", marginTop: 60 }}>
            <div style={{ fontSize: 52, marginBottom: 20 }}>📚</div>
            <div style={{
              fontSize: 22,
              fontWeight: 700,
              background: "linear-gradient(135deg, #00C8C8, #0080FF)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
              marginBottom: 8,
            }}>
              Ask anything about your TUM lectures
            </div>
            <div style={{ color: "#3a3a4a", fontSize: 14, marginBottom: 28 }}>
              Powered by Gemini Vision · Qwen3 Embeddings · Socratic AI
            </div>
            <div style={{ display: "flex", gap: 8, justifyContent: "center", flexWrap: "wrap" }}>
              {[
                "What is virtualization?",
                "Wie funktioniert Paging?",
                "Explain hypervisor",
                "Was ist ein Prozess?",
              ].map(q => (
                <button key={q} className="msg-btn" onClick={() => setInput(q)} style={{
                  padding: "9px 18px",
                  borderRadius: 22,
                  border: "1px solid #222",
                  background: "#111",
                  color: "#555",
                  cursor: "pointer",
                  fontSize: 13,
                  transition: "all 0.2s",
                }}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} style={{
            display: "flex",
            flexDirection: "column",
            alignItems: msg.role === "user" ? "flex-end" : "flex-start",
            gap: 8,
          }}>
            {/* Avatar label */}
            <div style={{
              fontSize: 11,
              color: "#3a3a4a",
              fontWeight: 600,
              letterSpacing: "0.05em",
              paddingLeft: msg.role === "user" ? 0 : 4,
              paddingRight: msg.role === "user" ? 4 : 0,
            }}>
              {msg.role === "user" ? "YOU" : "REPLYX AI"}
            </div>

            <div style={{
              maxWidth: "78%",
              padding: "14px 18px",
              borderRadius: msg.role === "user"
                ? "20px 20px 6px 20px"
                : "20px 20px 20px 6px",
              background: msg.role === "user"
                ? "linear-gradient(135deg, #00C8C8, #0080FF)"
                : "#14141a",
              border: msg.role === "user" ? "none" : "1px solid #1e1e2a",
              color: "#fff",
              fontSize: 15,
              lineHeight: 1.7,
              whiteSpace: "pre-wrap",
            }}>
              {msg.content}
            </div>

            {/* Sources */}
            {msg.sources && msg.sources.length > 0 && (
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", paddingLeft: 4 }}>
                {msg.sources.map((s: any, j: number) => (
                  <span key={j} style={{
                    padding: "4px 12px",
                    borderRadius: 14,
                    background: "rgba(0,200,200,0.05)",
                    border: "1px solid rgba(0,200,200,0.2)",
                    fontSize: 11,
                    color: "#00C8C8",
                    fontWeight: 500,
                  }}>
                    📄 {s.pdf} · Slide {s.slide_num}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}

        {loading && (
          <div style={{ display: "flex", gap: 6, paddingLeft: 4, alignItems: "center" }}>
            {[0, 1, 2].map(i => (
              <div key={i} style={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                background: "#00C8C8",
                animation: `pulse 1.2s ease-in-out ${i * 0.2}s infinite`,
              }} />
            ))}
            <span style={{ color: "#3a3a4a", fontSize: 13, marginLeft: 6 }}>
              ReplyX is thinking...
            </span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div style={{
        padding: "16px 24px 24px",
        borderTop: "1px solid #1a1a1f",
        maxWidth: 760,
        width: "100%",
        margin: "0 auto",
      }}>
        <div style={{
          display: "flex",
          gap: 10,
          background: "#14141a",
          border: "1px solid #1e1e2a",
          borderRadius: 28,
          padding: "6px 6px 6px 18px",
          transition: "border-color 0.2s",
        }}>
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === "Enter" && !e.shiftKey && sendMessage()}
            placeholder={mode === "tutor"
              ? "Ask a question from your lectures..."
              : "Search lecture slides..."}
            style={{
              flex: 1,
              background: "transparent",
              border: "none",
              color: "#fff",
              fontSize: 15,
              outline: "none",
              padding: "8px 0",
            }}
          />
          <button
            onClick={sendMessage}
            disabled={loading}
            className="send-btn"
            style={{
              padding: "10px 22px",
              borderRadius: 22,
              border: "none",
              background: loading
                ? "#1e1e2a"
                : "linear-gradient(135deg, #00C8C8, #0080FF)",
              color: loading ? "#555" : "#fff",
              fontWeight: 700,
              cursor: loading ? "not-allowed" : "pointer",
              fontSize: 14,
              transition: "all 0.2s",
              whiteSpace: "nowrap",
            }}
          >
            {loading ? "···" : "Send →"}
          </button>
        </div>
        <div style={{ textAlign: "center", marginTop: 10, color: "#2a2a3a", fontSize: 11 }}>
          103 slides indexed · Gemini 2.5 Flash · Qwen3 Embeddings
        </div>
      </div>
    </div>
  );
}