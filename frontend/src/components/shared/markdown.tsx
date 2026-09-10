import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function Markdown({ content }: { content: string }) {
  return (
    <div className="space-y-2 text-sm leading-relaxed break-words">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ ...props }) => (
            <a {...props} target="_blank" rel="noreferrer noopener" className="underline" />
          ),
          // Tailwind's preflight strips heading/table/quote styling — restore enough
          // hierarchy for chat-sized content.
          h1: ({ ...props }) => <h1 className="mt-1 text-base font-semibold" {...props} />,
          h2: ({ ...props }) => <h2 className="mt-1 text-base font-semibold" {...props} />,
          h3: ({ ...props }) => <h3 className="mt-1 text-sm font-semibold" {...props} />,
          h4: ({ ...props }) => <h4 className="mt-1 text-sm font-semibold" {...props} />,
          h5: ({ ...props }) => <h5 className="mt-1 text-sm font-semibold" {...props} />,
          h6: ({ ...props }) => (
            <h6 className="text-muted-foreground mt-1 text-sm font-semibold" {...props} />
          ),
          blockquote: ({ ...props }) => (
            <blockquote
              className="border-border text-muted-foreground border-l-2 pl-3"
              {...props}
            />
          ),
          hr: () => <hr className="border-border" />,
          code: ({ className, children, ...props }) => (
            <code
              className={`bg-muted rounded px-1 py-0.5 font-mono text-xs ${className ?? ''}`}
              {...props}
            >
              {children}
            </code>
          ),
          // Fenced blocks own the background; clear the inline pill on the nested <code>.
          pre: ({ children }) => (
            <pre className="bg-muted overflow-x-auto rounded p-3 text-xs [&_code]:bg-transparent [&_code]:p-0">
              {children}
            </pre>
          ),
          ul: ({ ...props }) => <ul className="list-disc pl-5" {...props} />,
          ol: ({ ...props }) => <ol className="list-decimal pl-5" {...props} />,
          table: ({ ...props }) => (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-xs" {...props} />
            </div>
          ),
          th: ({ ...props }) => (
            <th className="border-border border px-2 py-1 text-left font-medium" {...props} />
          ),
          td: ({ ...props }) => <td className="border-border border px-2 py-1" {...props} />,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
