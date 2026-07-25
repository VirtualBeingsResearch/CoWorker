import { Bot, CheckCircle2, ChevronUp, FileDiff, Info, Lightbulb, ListChecks, MessageSquare, Sparkles, Terminal, Users, XCircle } from "lucide-react";
import { isValidElement, useEffect, useState, type JSX } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import type { TimelineMessage } from "../lib/bridgeLogic";
import { useI18n } from "../i18n";
import { readDesktopImagePreview } from "../tauri";

type MdProps<T extends keyof JSX.IntrinsicElements> = JSX.IntrinsicElements[T] & { node?: unknown };

function safeUrlTransform(href: string, key: string) {
  const trimmed = href.trim();
  if (/^(https?:|mailto:)/i.test(trimmed)) return trimmed;
  return key === "src" && markdownLocalImagePath(trimmed) ? trimmed : "";
}

export function markdownLocalImagePath(source: string) {
  let decoded: string;
  try {
    decoded = decodeURI(source.trim());
  } catch {
    return null;
  }
  if (/^\/[a-z]:\//i.test(decoded)) return decoded.slice(1);
  if (/^[a-z]:[\\/]/i.test(decoded) || decoded.startsWith("/")) return decoded;
  return null;
}

function imageMediaType(path: string) {
  const extension = path.split(".").pop()?.toLowerCase();
  if (extension === "jpg" || extension === "jpeg") return "image/jpeg";
  if (extension === "svg") return "image/svg+xml";
  return extension ? `image/${extension}` : "application/octet-stream";
}

function MarkdownImage({ node, src = "", alt = "", ...props }: MdProps<"img">) {
  const localPath = markdownLocalImagePath(src);
  const [preview, setPreview] = useState<{ path: string; url: string } | null>(null);
  const [failedPath, setFailedPath] = useState("");

  useEffect(() => {
    if (!localPath) return;
    let disposed = false;
    let objectUrl = "";
    setFailedPath("");
    void readDesktopImagePreview(localPath)
      .then((bytes) => {
        if (disposed) return;
        objectUrl = URL.createObjectURL(new Blob([bytes], { type: imageMediaType(localPath) }));
        setPreview({ path: localPath, url: objectUrl });
      })
      .catch(() => {
        if (!disposed) setFailedPath(localPath);
      });
    return () => {
      disposed = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [localPath]);

  if (!src) return alt ? <span className="messageImageUnavailable">{alt}</span> : null;
  if (!localPath) return <img {...props} alt={alt} loading="lazy" src={src} />;
  if (failedPath === localPath) {
    return <span className="messageImageUnavailable" role="img" aria-label={alt}>{alt}</span>;
  }
  if (preview?.path !== localPath) {
    return <span className="messageImageLoading" role="status" aria-label={alt} />;
  }
  return <img {...props} alt={alt} className="messageMarkdownImage" src={preview.url} />;
}

// Clamp every heading level into h4-h6 so headings never outgrow the chat
// bubble; matches the visual scale the hand-rolled parser used to enforce.
function MarkdownH1({ node, ...props }: MdProps<"h1">) {
  return <h4 {...props} />;
}
function MarkdownH2({ node, ...props }: MdProps<"h2">) {
  return <h5 {...props} />;
}
function MarkdownH3({ node, ...props }: MdProps<"h3">) {
  return <h6 {...props} />;
}
function MarkdownH4({ node, ...props }: MdProps<"h4">) {
  return <h6 {...props} />;
}
function MarkdownH5({ node, ...props }: MdProps<"h5">) {
  return <h6 {...props} />;
}
function MarkdownH6({ node, ...props }: MdProps<"h6">) {
  return <h6 {...props} />;
}

function MarkdownLink({ node, ...props }: MdProps<"a">) {
  return <a {...props} target="_blank" rel="noreferrer" />;
}

function MarkdownPre({ node, children, ...props }: MdProps<"pre">) {
  const codeChild = Array.isArray(children) ? children[0] : children;
  const codeClassName = isValidElement<{ className?: string }>(codeChild) ? codeChild.props.className ?? "" : "";
  const language = /language-(\S+)/.exec(codeClassName)?.[1] ?? "";
  return (
    <pre {...props} className="messageCodeBlock">
      {language && <span>{language}</span>}
      {children}
    </pre>
  );
}

const markdownComponents: Components = {
  h1: MarkdownH1,
  h2: MarkdownH2,
  h3: MarkdownH3,
  h4: MarkdownH4,
  h5: MarkdownH5,
  h6: MarkdownH6,
  a: MarkdownLink,
  img: MarkdownImage,
  pre: MarkdownPre,
};

export function MessageText({ text }: { text: string }) {
  return (
    <div className="messageText">
      <ReactMarkdown
        // singleDollarTextMath: false — shell vars/prices like `$HOME` or "$5-$10" in
        // plain prose otherwise get greedily parsed as inline math. $$...$$ still works.
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: false }]]}
        rehypePlugins={[rehypeKatex]}
        urlTransform={safeUrlTransform}
        components={markdownComponents}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

export function ToolResultDisclosure({ result }: { result: TimelineMessage }) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="messageToolResult">
      <button
        aria-expanded={expanded}
        className="messageToolResultToggle"
        onClick={() => setExpanded((current) => !current)}
        type="button"
      >
        <ChevronUp size={14} style={{ transform: expanded ? undefined : "rotate(180deg)" }} />
        {result.is_error ? <XCircle className="toolResultStatus error" size={13} /> : <CheckCircle2 className="toolResultStatus success" size={13} />}
        <span className="messageToolResultLabel">{result.author_label}</span>
        {!expanded && <span className="messageToolResultHint">{t("common.clickToExpand")}</span>}
      </button>
      {expanded && <MessageText text={result.text} />}
    </div>
  );
}

export function MessageIcon({ message }: { message: TimelineMessage }) {
  switch (message.kind) {
    case "bubble_handoff":
    case "bubble_reply":
      return <Sparkles size={16} />;
    case "reasoning":
      return <Lightbulb size={16} />;
    case "tool_call":
    case "tool_result":
      return <Terminal size={16} />;
    case "patch":
      return <FileDiff size={16} />;
    case "plan":
      return <ListChecks size={16} />;
    case "system":
      return <Info size={16} />;
    default:
      return message.author_kind === "codex" ? (
        <Bot size={17} />
      ) : message.author_kind === "coworker" ? (
        <Users size={17} />
      ) : (
        <MessageSquare size={16} />
      );
  }
}
