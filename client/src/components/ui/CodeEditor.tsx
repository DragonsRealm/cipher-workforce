import React from 'react';
import Editor from 'react-simple-code-editor';
import Prism from 'prismjs';
import 'prismjs/components/prism-python';
import 'prismjs/components/prism-javascript';
import 'prismjs/components/prism-typescript';
import 'prismjs/components/prism-markdown';

interface CodeEditorProps {
  value: string;
  onChange: (value: string) => void;
  language?: string;
  placeholder?: string;
}

const CodeEditor: React.FC<CodeEditorProps> = ({
  value,
  onChange,
  language = 'python',
  placeholder
}) => {
  const handleChange = (newValue: string) => {
    onChange(newValue);
  };

  const highlight = (code: string) => {
    const grammar = Prism.languages[language] || Prism.languages.python;
    return Prism.highlight(code, grammar, language);
  };

  return (
    <div className="h-full overflow-auto rounded-md border border-border">
      <div className="code-editor-container h-full bg-muted text-foreground">
        <Editor
          value={value || ''}
          onValueChange={handleChange}
          highlight={highlight}
          placeholder={placeholder}
          padding={12}
          tabSize={4}
          insertSpaces={true}
          // Underlying textarea cannot use Tailwind classes for these
          // properties; font + sizing must live on the editor's style
          // prop. Colors come from .code-editor-container in index.css.
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 13,
            lineHeight: 1.5,
            minHeight: '100%',
            height: 'auto',
            overflow: 'visible',
          }}
        />
      </div>
    </div>
  );
};

export default CodeEditor;
