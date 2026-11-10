import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useScenarioTasks } from '@/features/scenarios/queries'
import { FormField } from '@/components/shared/form-field'
import { Button } from '@/components/ui/button'
import { Modal } from '@/components/ui/modal'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

type ScenarioOption = { id: string; name: string; description?: string | null }

// The first step of starting a conversation: which scenario it is an attempt at. The rest — models,
// parameters — is the New conversation page, which this hands the choice to.
export function StartConversationDialog({
  evaluationId,
  scenarios,
  open,
  onOpenChange,
}: {
  evaluationId: string
  scenarios: ScenarioOption[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const navigate = useNavigate()
  const [scenarioId, setScenarioId] = useState('')
  const scenario = scenarios.find((s) => s.id === scenarioId)
  const tasks = useScenarioTasks(scenario?.id ?? '')

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="Start a conversation"
      onOpen={() => setScenarioId('')}
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault()
          if (scenario)
            navigate(`/evaluations/${evaluationId}/conversation-groups/new?scenario=${scenario.id}`)
        }}
      >
        <FormField label="Scenario" htmlFor="start-scenario">
          <Select value={scenarioId} onValueChange={setScenarioId}>
            <SelectTrigger id="start-scenario">
              <SelectValue placeholder="Select a scenario…" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {scenarios.map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </FormField>
        {scenario && (
          <div className="space-y-2 text-sm">
            {scenario.description && (
              <p className="text-muted-foreground whitespace-pre-line">{scenario.description}</p>
            )}
            {(tasks.data?.items.length ?? 0) > 0 && (
              <div>
                <p className="font-medium">Tasks in this scenario</p>
                <ul className="text-muted-foreground list-disc pl-5">
                  {tasks.data?.items.map((t) => (
                    <li key={t.id}>{t.name}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" disabled={!scenario}>
            Continue
          </Button>
        </div>
      </form>
    </Modal>
  )
}
