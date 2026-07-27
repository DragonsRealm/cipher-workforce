/**
 * Workflow serialization + structural pre-flight.
 *
 * The 166-line `WorkflowJSONSchema` draft-07 document that used to head
 * this file was deleted: nothing referenced it, and it had drifted from
 * the real shape. The backend is the schema authority.
 *
 * `validateWorkflow` below stays deliberately — it is a structural check on
 * a file the user is about to write to disk, where a server round-trip is
 * the wrong shape.
 */

/**
 * Validates a workflow object against the JSON schema
 */
export function validateWorkflow(workflow: any): { valid: boolean; errors: string[] } {
  const errors: string[] = [];

  if (!workflow.id || typeof workflow.id !== 'string') {
    errors.push('Workflow must have a valid id');
  }

  if (!workflow.name || typeof workflow.name !== 'string') {
    errors.push('Workflow must have a valid name');
  }

  if (!Array.isArray(workflow.nodes)) {
    errors.push('Workflow must have a nodes array');
  }

  if (!Array.isArray(workflow.edges)) {
    errors.push('Workflow must have an edges array');
  }

  if (!workflow.createdAt) {
    errors.push('Workflow must have a createdAt timestamp');
  }

  if (!workflow.lastModified) {
    errors.push('Workflow must have a lastModified timestamp');
  }

  workflow.nodes?.forEach((node: any, index: number) => {
    if (!node.id) {
      errors.push(`Node at index ${index} must have an id`);
    }
    if (!node.type) {
      errors.push(`Node at index ${index} must have a type`);
    }
    if (!node.position || typeof node.position.x !== 'number' || typeof node.position.y !== 'number') {
      errors.push(`Node at index ${index} must have a valid position with x and y coordinates`);
    }
  });

  workflow.edges?.forEach((edge: any, index: number) => {
    if (!edge.id) {
      errors.push(`Edge at index ${index} must have an id`);
    }
    if (!edge.source) {
      errors.push(`Edge at index ${index} must have a source node`);
    }
    if (!edge.target) {
      errors.push(`Edge at index ${index} must have a target node`);
    }

    const sourceExists = workflow.nodes?.some((n: any) => n.id === edge.source);
    const targetExists = workflow.nodes?.some((n: any) => n.id === edge.target);

    if (!sourceExists) {
      errors.push(`Edge ${edge.id} references non-existent source node: ${edge.source}`);
    }
    if (!targetExists) {
      errors.push(`Edge ${edge.id} references non-existent target node: ${edge.target}`);
    }
  });

  return {
    valid: errors.length === 0,
    errors
  };
}

/**
 * Serializes a workflow to JSON format
 */
export function serializeWorkflow(workflow: any): string {
  return JSON.stringify(workflow, null, 2);
}

/**
 * Deserializes a JSON string to workflow object
 */
export function deserializeWorkflow(json: string): any {
  try {
    const workflow = JSON.parse(json);

    if (workflow.createdAt) {
      workflow.createdAt = new Date(workflow.createdAt);
    }
    if (workflow.lastModified) {
      workflow.lastModified = new Date(workflow.lastModified);
    }

    return workflow;
  } catch (error) {
    throw new Error(`Failed to parse workflow JSON: ${error}`, { cause: error });
  }
}
