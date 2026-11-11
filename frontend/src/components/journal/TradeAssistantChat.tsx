import { useState } from 'react';
import { ArrowUp, Bot, MoreVertical, Trash2 } from 'lucide-react';
import { Card } from '@/components/ui/card';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { cn, formatINR, formatPrice } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import type { ChatMessage, JournalReport } from '@/types/trading';

interface TradeAssistantChatProps {
  report: JournalReport;
}

function generateContextualReply(report: JournalReport, prompt: string): Omit<ChatMessage, 'id' | 'timestamp'> {
  const lower = prompt.toLowerCase();

  if (lower.includes('improve') || lower.includes('entry')) {
    return {
      role: 'assistant',
      content: `Reviewing ${report.instrument} (${report.tradeId}) entry at ₹${formatPrice(report.entryPrice, 2)}:`,
      bullets: [
        `Strategy ${report.strategy} executed at ${report.entryTime} with stop ₹${formatPrice(report.stopPrice, 2)}.`,
        'Waiting for a 5m pullback retest after the breakout close could tighten initial stop distance by ~15–20%.',
        'Confirming relative volume ≥ 1.3x before entry improves repeatability across sessions.',
      ],
      closingNote: 'Keep entry criteria rule-based rather than adjusting stops mid-trade.',
    };
  }

  if (lower.includes('losing') || lower.includes('compare') || lower.includes('fail')) {
    return {
      role: 'assistant',
      content: `Comparing ${report.tradeId} (${report.riskReward}) against recent stopped trades (#TR-0020 HDFCBANK, #TR-0018 NIFTY 50):`,
      bullets: [
        `${report.strategy} trades aligned with the broader index direction achieved 68% win rate vs 38% for counter-trend setups.`,
        `Your planned risk of ${formatINR(report.plannedRisk)} stayed well within the 1.00% Risk per Trade threshold.`,
        'Losing trades occurred primarily when fading opening breadth on strong index sessions.',
      ],
      closingNote: 'Filtering out counter-trend shorts during >1% index days is your highest-leverage refinement.',
    };
  }

  if (lower.includes('size') || lower.includes('risk') || lower.includes('stop')) {
    return {
      role: 'assistant',
      content: `Position sizing breakdown for ${report.instrument} ${report.tradeId}:`,
      bullets: [
        `Quantity: ${report.quantity} units · Planned risk: ${formatINR(report.plannedRisk)}.`,
        `Realised outcome: ${formatINR(report.grossPnl, { showSign: true })} (${report.riskReward}).`,
        `Risk remained aligned with workspace max risk-per-trade constraints.`,
      ],
      closingNote: 'Consistent R-based sizing prevents single-trade variance from skewing monthly returns.',
    };
  }

  return {
    role: 'assistant',
    content: `Here is the contextual breakdown for ${report.instrument} (${report.tradeId}):`,
    bullets: [
      `Direction & Strategy: ${report.direction} via ${report.strategy} (${report.timeInTrade} duration).`,
      `Execution: Entry ₹${formatPrice(report.entryPrice, 2)} -> Exit ₹${formatPrice(report.exitPrice, 2)} (${report.riskReward}).`,
      `Takeaway: ${report.insights[0]?.title ?? 'Rule adherence was maintained throughout the trade.'}`,
    ],
    closingNote: 'Ask about entry timing, stop placement, or comparisons with other trades in this journal.',
  };
}

export function TradeAssistantChat({ report }: TradeAssistantChatProps) {
  const [activeTab, setActiveTab] = useState<'chat' | 'context'>('chat');
  const [input, setInput] = useState('');
  const [isTyping, setIsTyping] = useState(false);

  const journalChats = useAppStore((s) => s.journalChats);
  const appendJournalMessage = useAppStore((s) => s.appendJournalMessage);
  const clearJournalChat = useAppStore((s) => s.clearJournalChat);

  const messages = journalChats[report.tradeId] ?? report.initialMessages;

  const submitQuestion = (questionText: string) => {
    const trimmed = questionText.trim();
    if (!trimmed || isTyping) return;

    const now = `${new Date().toTimeString().slice(0, 5)} IST`;
    appendJournalMessage(report.tradeId, {
      id: `user-${Date.now()}`,
      role: 'user',
      content: trimmed,
      timestamp: now,
    });
    setInput('');
    setIsTyping(true);

    setTimeout(() => {
      const reply = generateContextualReply(report, trimmed);
      appendJournalMessage(report.tradeId, {
        ...reply,
        id: `assistant-${Date.now()}`,
        timestamp: now,
      });
      setIsTyping(false);
    }, 320);
  };

  return (
    <Card className="flex h-full min-h-[580px] flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-[#E4E4E7] px-4 py-3.5">
        <div className="flex items-center gap-2">
          <Bot className="h-4 w-4 text-[#52647A]" strokeWidth={1.8} />
          <h3 className="text-[13.5px] font-semibold text-[#18181B]">Trade assistant</h3>
        </div>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="rounded-md p-1 text-[#71717A] hover:bg-[#F4F4F5] hover:text-[#18181B]"
              aria-label="Assistant options"
            >
              <MoreVertical className="h-4 w-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={() => clearJournalChat(report.tradeId)}>
              <Trash2 className="h-3.5 w-3.5 text-[#71717A]" />
              Clear conversation
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* Chat / Context Tabs */}
      <div className="flex items-center gap-4 border-b border-[#E4E4E7] px-4">
        <button
          type="button"
          onClick={() => setActiveTab('chat')}
          className={cn(
            'border-b-2 py-2.5 text-[12.5px] font-medium transition-colors',
            activeTab === 'chat'
              ? 'border-[#52647A] text-[#18181B]'
              : 'border-transparent text-[#71717A] hover:text-[#18181B]'
          )}
        >
          Chat
        </button>
        <button
          type="button"
          onClick={() => setActiveTab('context')}
          className={cn(
            'border-b-2 py-2.5 text-[12.5px] font-medium transition-colors',
            activeTab === 'context'
              ? 'border-[#52647A] text-[#18181B]'
              : 'border-transparent text-[#71717A] hover:text-[#18181B]'
          )}
        >
          Context
        </button>
      </div>

      {/* Body */}
      {activeTab === 'context' ? (
        <div className="flex-1 space-y-3 overflow-y-auto p-4 text-[12px]">
          <p className="text-[11px] font-medium uppercase tracking-wider text-[#71717A]">
            Attached Report Context
          </p>
          <div className="space-y-2 rounded-lg border border-[#E4E4E7] bg-[#F9FAFB] p-3">
            <div className="flex justify-between">
              <span className="text-[#71717A]">Instrument</span>
              <span className="font-semibold text-[#18181B]">{report.instrument}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[#71717A]">Trade ID</span>
              <span className="font-mono text-[#18181B]">{report.tradeId}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[#71717A]">Strategy</span>
              <span className="text-[#18181B]">{report.strategy}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[#71717A]">Entry / Exit</span>
              <span className="text-[#18181B] tabular-nums">
                ₹{formatPrice(report.entryPrice, 2)} → ₹{formatPrice(report.exitPrice, 2)}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-[#71717A]">Stop / Target</span>
              <span className="text-[#18181B] tabular-nums">
                ₹{formatPrice(report.stopPrice, 2)} / ₹{formatPrice(report.plannedTarget, 2)}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-[#71717A]">R-Multiple</span>
              <span className="font-semibold text-[#18181B]">{report.riskReward}</span>
            </div>
          </div>
          <p className="text-[11.5px] leading-relaxed text-[#71717A]">
            All responses in the Chat tab automatically reference this trade&apos;s entry rationale, stop distance, and realised outcome.
          </p>
        </div>
      ) : (
        <>
          <div className="flex-1 space-y-4 overflow-y-auto p-4">
            {/* Attached Context Chip */}
            <div className="rounded-lg border border-[#E4E4E7] bg-[#F9FAFB] px-3 py-1.5 font-mono text-[11px] text-[#52647A]">
              {report.instrument} · {report.tradeId}
            </div>

            {messages.length === 0 && (
              <p className="py-6 text-center text-xs text-[#71717A]">
                Conversation cleared. Ask a question about {report.instrument} {report.tradeId} below.
              </p>
            )}

            {messages.map((msg) =>
              msg.role === 'user' ? (
                <div key={msg.id} className="flex justify-end">
                  <div className="max-w-[85%] rounded-xl bg-[#F1F5F9] px-3.5 py-2.5 text-[12.5px] text-[#18181B]">
                    {msg.content}
                  </div>
                </div>
              ) : (
                <div key={msg.id} className="space-y-2 text-[12.5px] text-[#18181B]">
                  <div className="flex items-center gap-1.5 text-[11.5px] font-semibold text-[#18181B]">
                    <Bot className="h-3.5 w-3.5 text-[#52647A]" />
                    <span>Trade assistant</span>
                  </div>
                  <p className="leading-relaxed text-[#3F3F46]">{msg.content}</p>
                  {msg.bullets && msg.bullets.length > 0 && (
                    <ul className="space-y-1 pl-4 text-[#3F3F46] list-disc">
                      {msg.bullets.map((b, idx) => (
                        <li key={idx} className="leading-relaxed">
                          {b}
                        </li>
                      ))}
                    </ul>
                  )}
                  {msg.closingNote && (
                    <p className="leading-relaxed text-[#52525B]">{msg.closingNote}</p>
                  )}
                </div>
              )
            )}

            {isTyping && (
              <div className="flex items-center gap-1.5 text-[12px] text-[#71717A]">
                <Bot className="h-3.5 w-3.5 animate-pulse text-[#52647A]" />
                <span>Analyzing {report.tradeId}...</span>
              </div>
            )}
          </div>

          {/* Suggested Follow-ups + Input */}
          <div className="border-t border-[#F4F4F5] p-3.5 space-y-3">
            {report.suggestedFollowUps.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-[11px] font-medium text-[#71717A]">
                  Suggested follow-ups
                </p>
                {report.suggestedFollowUps.map((q) => (
                  <button
                    key={q}
                    type="button"
                    onClick={() => submitQuestion(q)}
                    className="block w-full rounded-lg border border-[#E4E4E7] bg-white px-3 py-2 text-left text-[12px] text-[#52525B] transition-colors hover:bg-[#F9FAFB] hover:text-[#18181B]"
                  >
                    {q}
                  </button>
                ))}
              </div>
            )}

            <form
              onSubmit={(e) => {
                e.preventDefault();
                submitQuestion(input);
              }}
              className="rounded-xl border border-[#E4E4E7] bg-[#F9FAFB] p-2.5 focus-within:border-[#52647A] focus-within:bg-white"
            >
              <input
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Ask about this trade..."
                className="w-full bg-transparent text-[12.5px] text-[#18181B] placeholder:text-[#71717A] focus:outline-none"
              />
              <div className="mt-2 flex items-center justify-between">
                <span className="text-[10.5px] text-[#71717A]">
                  Trade context attached
                </span>
                <button
                  type="submit"
                  disabled={!input.trim() || isTyping}
                  className="flex h-7 w-7 items-center justify-center rounded-lg bg-[#52647A] text-white transition-colors hover:bg-[#435367] disabled:opacity-40"
                  aria-label="Send message"
                >
                  <ArrowUp className="h-3.5 w-3.5" />
                </button>
              </div>
            </form>

            <p className="text-center text-[10.5px] text-[#71717A]">
              AI can make mistakes. Review every insight.
            </p>
          </div>
        </>
      )}
    </Card>
  );
}
