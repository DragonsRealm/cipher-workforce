/* eslint-disable react-hooks/incompatible-library -- react-hook-form's watch()
   cannot be memoized safely; the compiler (>= 1.0.0) skips this component
   rather than miscompiling it, so the warning is advisory. Not true before
   1.0.0 — do not downgrade. */
import React, { useEffect } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import Modal from './ui/Modal';
import CodeEditor from './ui/CodeEditor';
import { useWebSocket } from '../contexts/WebSocketContext';
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import { ActionButton } from '@/components/ui/action-button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Switch } from '@/components/ui/switch';

// ---------------------------------------------------------------------------
// Schema (one source of truth for type, defaults, and validation).
// ---------------------------------------------------------------------------

const userSkillSchema = z.object({
  id: z.number().optional(),
  name: z
    .string()
    .min(1, 'Internal name is required')
    .regex(/^[a-z0-9-]+$/, 'Use lowercase letters, digits, and hyphens only'),
  display_name: z.string().min(1, 'Display name is required'),
  description: z.string().min(1, 'Description is required'),
  instructions: z.string().min(1, 'Instructions are required'),
  allowed_tools: z.array(z.string()).default([]),
  category: z.string().default('custom'),
  icon: z.string().default('star'),
  color: z.string().default('#6366F1'),
  is_active: z.boolean().default(true),
});

export type UserSkill = z.infer<typeof userSkillSchema>;

const DEFAULT_SKILL: UserSkill = userSkillSchema.parse({
  name: '',
  display_name: '',
  description: '',
  instructions: `# My Custom Skill

## Capabilities
- Describe what this skill can do
- List the main functions

## Usage
Explain when and how the Zeenie should use this skill.

## Examples

**User**: "Example request"
**Action**: Describe what the skill does in response
`,
});

const CATEGORY_OPTIONS = [
  { value: 'custom', label: 'Custom' },
  { value: 'communication', label: 'Communication' },
  { value: 'productivity', label: 'Productivity' },
  { value: 'automation', label: 'Automation' },
  { value: 'integration', label: 'Integration' },
  { value: 'utility', label: 'Utility' },
];

const ICON_OPTIONS = [
  { value: 'star', label: 'Star' },
  { value: 'sparkles', label: 'Sparkles' },
  { value: 'brain', label: 'Brain' },
  { value: 'code', label: 'Code' },
  { value: 'globe', label: 'Globe' },
  { value: 'chat', label: 'Chat' },
  { value: 'calendar', label: 'Calendar' },
  { value: 'settings', label: 'Settings' },
];

const COLOR_OPTIONS = [
  '#6366F1', '#8B5CF6', '#EC4899', '#EF4444',
  '#F59E0B', '#10B981', '#3B82F6', '#06B6D4',
];

interface SkillEditorModalProps {
  isOpen: boolean;
  onClose: () => void;
  skill?: UserSkill | null;
  onSave?: (skill: UserSkill) => void;
}

const SkillEditorModal: React.FC<SkillEditorModalProps> = ({
  isOpen,
  onClose,
  skill,
  onSave,
}) => {
  const { sendRequest } = useWebSocket();
  const [activeTab, setActiveTab] = React.useState<'details' | 'instructions'>('details');
  const [submitError, setSubmitError] = React.useState<string | null>(null);

  const form = useForm({
    resolver: zodResolver(userSkillSchema),
    defaultValues: skill ?? DEFAULT_SKILL,
    mode: 'onSubmit',
  });

  // Reset form when modal opens or the edited skill changes.
  useEffect(() => {
    if (!isOpen) return;
    form.reset(skill ?? DEFAULT_SKILL);
    setSubmitError(null);
  }, [isOpen, skill, form]);

  const handleDisplayNameChange = (value: string) => {
    form.setValue('display_name', value, { shouldValidate: false, shouldDirty: true });
    // Auto-derive internal name only for new skills (no id).
    if (!form.getValues('id')) {
      const derived = value.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '');
      form.setValue('name', derived, { shouldValidate: false, shouldDirty: true });
    }
  };

  const onSubmit = async (values: UserSkill) => {
    setSubmitError(null);
    try {
      const messageType = values.id ? 'update_user_skill' : 'create_user_skill';
      const response = await sendRequest<{ success?: boolean; skill?: UserSkill; error?: string }>(
        messageType,
        { ...values, allowed_tools: values.allowed_tools.join(',') },
      );
      if (response?.success) {
        onSave?.(response.skill ?? values);
        onClose();
      } else {
        setSubmitError(response?.error || 'Failed to save skill');
      }
    } catch (err: any) {
      setSubmitError(err?.message || 'Failed to save skill');
    }
  };

  const isSaving = form.formState.isSubmitting;
  const isExisting = !!form.watch('id');
  const tabBaseClass =
    'px-4 py-2 text-sm font-medium border-b-2 border-transparent transition-colors';
  const tabActiveClass = 'border-primary text-foreground';
  const tabInactiveClass = 'text-muted-foreground hover:text-foreground';

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={isExisting ? 'Edit Skill' : 'Create Skill'}
      maxWidth="700px"
      maxHeight="85vh"
    >
      <Form {...form}>
        <form
          onSubmit={form.handleSubmit(onSubmit)}
          className="flex h-full flex-col"
        >
          {/* Tabs */}
          <div className="flex border-b border-border bg-background">
            <button
              type="button"
              className={`${tabBaseClass} ${activeTab === 'details' ? tabActiveClass : tabInactiveClass}`}
              onClick={() => setActiveTab('details')}
            >
              Details
            </button>
            <button
              type="button"
              className={`${tabBaseClass} ${activeTab === 'instructions' ? tabActiveClass : tabInactiveClass}`}
              onClick={() => setActiveTab('instructions')}
            >
              Instructions
            </button>
          </div>

          <div className="flex-1 overflow-auto p-4">
            {activeTab === 'details' && (
              <div className="flex flex-col gap-4">
                <FormField
                  control={form.control}
                  name="display_name"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Display Name *</FormLabel>
                      <FormControl>
                        <Input
                          {...field}
                          placeholder="My Custom Skill"
                          onChange={(e) => handleDisplayNameChange(e.target.value)}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <FormField
                  control={form.control}
                  name="name"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Internal Name *</FormLabel>
                      <FormControl>
                        <Input
                          {...field}
                          placeholder="my-custom-skill"
                          readOnly={isExisting}
                          className={isExisting ? 'opacity-70' : undefined}
                          onChange={(e) =>
                            field.onChange(e.target.value.toLowerCase().replace(/\s+/g, '-'))
                          }
                        />
                      </FormControl>
                      <FormDescription>Used internally to identify the skill.</FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <FormField
                  control={form.control}
                  name="description"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Description *</FormLabel>
                      <FormControl>
                        <Textarea
                          {...field}
                          rows={3}
                          placeholder="A short description of what this skill does..."
                        />
                      </FormControl>
                      <FormDescription>
                        Shown in the skill registry to help the agent decide when to use it.
                      </FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <div className="flex gap-4">
                  <FormField
                    control={form.control}
                    name="category"
                    render={({ field }) => (
                      <FormItem className="flex-1">
                        <FormLabel>Category</FormLabel>
                        <Select onValueChange={field.onChange} value={field.value}>
                          <FormControl>
                            <SelectTrigger>
                              <SelectValue />
                            </SelectTrigger>
                          </FormControl>
                          <SelectContent>
                            {CATEGORY_OPTIONS.map((opt) => (
                              <SelectItem key={opt.value} value={opt.value}>
                                {opt.label}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="color"
                    render={({ field }) => (
                      <FormItem className="flex-1">
                        <FormLabel>Color</FormLabel>
                        <div className="flex flex-wrap gap-1">
                          {COLOR_OPTIONS.map((color) => (
                            <button
                              key={color}
                              type="button"
                              onClick={() => field.onChange(color)}
                              className="h-7 w-7 rounded cursor-pointer"
                              style={{
                                backgroundColor: color,
                                border: field.value === color ? '2px solid white' : 'none',
                                boxShadow: field.value === color ? `0 0 0 2px ${color}` : 'none',
                              }}
                            />
                          ))}
                        </div>
                      </FormItem>
                    )}
                  />
                </div>

                <FormField
                  control={form.control}
                  name="icon"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Icon</FormLabel>
                      <Select onValueChange={field.onChange} value={field.value}>
                        <FormControl>
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                        </FormControl>
                        <SelectContent>
                          {ICON_OPTIONS.map((opt) => (
                            <SelectItem key={opt.value} value={opt.value}>
                              {opt.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </FormItem>
                  )}
                />

                <FormField
                  control={form.control}
                  name="is_active"
                  render={({ field }) => (
                    <FormItem className="flex items-center gap-2 space-y-0">
                      <FormControl>
                        <Switch checked={field.value} onCheckedChange={field.onChange} />
                      </FormControl>
                      <FormLabel className="!mt-0">Skill is active</FormLabel>
                    </FormItem>
                  )}
                />
              </div>
            )}

            {activeTab === 'instructions' && (
              <div className="flex h-full flex-col gap-3">
                <div className="text-sm text-muted-foreground">
                  Write markdown instructions for the agent. Include capabilities, usage
                  guidelines, and examples.
                </div>
                <FormField
                  control={form.control}
                  name="instructions"
                  render={({ field }) => (
                    <FormItem className="flex-1 min-h-[400px]">
                      <FormControl>
                        <CodeEditor
                          value={field.value}
                          onChange={field.onChange}
                          language="markdown"
                          placeholder={`# Skill Instructions

## Capabilities
- ...

## Usage
...

## Examples
...`}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </div>
            )}
          </div>

          {submitError && (
            <Alert variant="destructive" className="rounded-none border-x-0 border-b-0">
              <AlertDescription>{submitError}</AlertDescription>
            </Alert>
          )}

          <div className="flex justify-end gap-3 border-t border-border bg-card px-4 py-3">
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <ActionButton intent="save" type="submit" disabled={isSaving}>
              {isSaving ? 'Saving...' : isExisting ? 'Update Skill' : 'Create Skill'}
            </ActionButton>
          </div>
        </form>
      </Form>
    </Modal>
  );
};

export default SkillEditorModal;
