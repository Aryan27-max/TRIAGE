/**
 * A six-colour JSON view. A syntax-highlighting library is 40kb for this, and the
 * Inspector renders the API's real payloads — it does not invent a display format.
 */
function tokenise(value: string) {
  const pattern =
    /("(?:\.|[^"\])*"\s*:)|("(?:\.|[^"\])*")|(\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|(\btrue\b|\bfalse\b)|(\bnull\b)|([{}[\],])/g;
  const out: { text: string; cls: string }[] = [];
  let last = 0;
  for (const match of value.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > last) out.push({ text: value.slice(last, index), cls: "" });
    const cls = match[1]
      ? "tok-key"
      : match[2]
        ? "tok-str"
        : match[3]
          ? "tok-num"
          : match[4]
            ? "tok-bool"
            : match[5]
              ? "tok-null"
              : "tok-punct";
    out.push({ text: match[0], cls });
    last = index + match[0].length;
  }
  if (last < value.length) out.push({ text: value.slice(last), cls: "" });
  return out;
}

export function Json({ value, className }: { value: unknown; className?: string }) {
  const text = JSON.stringify(value, null, 2) ?? "null";
  return (
    <pre
      className={`overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.55] ${className ?? ""}`}
    >
      {tokenise(text).map((token, index) => (
        <span key={index} className={token.cls}>
          {token.text}
        </span>
      ))}
    </pre>
  );
}
