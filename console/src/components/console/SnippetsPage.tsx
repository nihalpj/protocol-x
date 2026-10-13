import { useState } from "react";
import { toast } from "sonner";
import { api, useApi } from "@/api";
import { copyText } from "@/lib/clipboard";
import type { Snippets } from "@/lib/types";

function SnippetBlock({ title, subtitle, text }: { title: string; subtitle: string; text: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    if (await copyText(text)) {
      setCopied(true);
      toast.success(`${title} copied to clipboard`);
      setTimeout(() => setCopied(false), 1500);
    } else {
      toast.error("copy failed — select the text manually");
    }
  };
  return (
    <div className="flex min-w-0 flex-col rounded-xl border border-[#e4e4e7] bg-white">
      <div className="flex items-center gap-2 border-b border-[#f4f4f5] px-3 py-2">
        <div>
          <div className="text-[12px] font-semibold">{title}</div>
          <div className="text-[10px] text-[#a1a1aa]">{subtitle}</div>
        </div>
        <button
          onClick={onCopy}
          className="ml-auto rounded-md border border-[#e4e4e7] bg-[#fafafa] px-2.5 py-1 text-[10.5px] font-medium text-[#3f3f46] transition-colors hover:bg-[#f4f4f5]"
        >
          {copied ? "copied ✓" : "⧉ copy"}
        </button>
      </div>
      <pre className="mono min-h-0 flex-1 overflow-auto bg-[#18181b] p-3 text-[10.5px] leading-relaxed text-[#e4e4e7] scroll-thin">
        {text}
      </pre>
    </div>
  );
}

function snippetsToText(snippets: Snippets): { claude: string; http: string } {
  return {
    claude: JSON.stringify(snippets.claude_desktop, null, 2),
    http: JSON.stringify(
      {
        url: snippets.generic_http.url,
        transport: snippets.generic_http.transport,
        headers: snippets.generic_http.headers,
      },
      null,
      2,
    ),
  };
}

export function SnippetsPage() {
  const snipQ = useApi(api.snippets, null);

  return (
    <section className="flex h-full min-w-0 flex-1 flex-col bg-[#fafafa]">
      <div className="flex items-center gap-2 border-b border-[#e4e4e7] bg-white px-3 py-2">
        <div className="text-[12px] font-semibold">Client snippets</div>
        <span className="mono rounded bg-[#f4f4f5] px-1.5 py-0.5 text-[10px] text-[#71717a]">
          connect an MCP client to this server
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3 scroll-thin">
        {snipQ.loading && <div className="py-10 text-center text-[11px] text-[#a1a1aa]">loading snippets…</div>}
        {snipQ.error && !snipQ.loading && (
          <div className="py-10 text-center text-[11px] text-[#b91c1c]">failed to load snippets: {snipQ.error}</div>
        )}
        {snipQ.data && (
          <div className="grid h-full grid-cols-1 gap-3 lg:grid-cols-2">
            <SnippetBlock
              title="Claude Desktop"
              subtitle="claude_desktop_config.json → mcpServers"
              text={snippetsToText(snipQ.data).claude}
            />
            <SnippetBlock
              title="Generic HTTP client"
              subtitle={`${snipQ.data.generic_http.transport} transport`}
              text={snippetsToText(snipQ.data).http}
            />
          </div>
        )}
      </div>
    </section>
  );
}
