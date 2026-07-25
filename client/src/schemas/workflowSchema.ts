/**
 * JSON Schema for Workflow Definition
 * Defines the structure and validation rules for workflow data
 */

export const WorkflowJSONSchema = {
  $schema: "http://json-schema.org/draft-07/schema#",
  title: "Workflow",
  description: "A workflow automation definition containing nodes and connections",
  type: "object",
  required: ["id", "name", "nodes", "edges", "createdAt", "lastModified"],
  properties: {
    id: {
      type: "string",
      description: "Unique identifier for the workflow",
      pattern: "^workflow_[0-9]+$"
    },
    name: {
      type: "string",
      description: "Human-readable workflow name",
      minLength: 1,
      maxLength: 100
    },
    description: {
      type: "string",
      description: "Optional workflow description"
    },
    nodes: {
      type: "array",
      description: "Array of workflow nodes",
      items: {
        $ref: "#/definitions/node"
      }
    },
    edges: {
      type: "array",
      description: "Array of connections between nodes",
      items: {
        $ref: "#/definitions/edge"
      }
    },
    createdAt: {
      type: "string",
      format: "date-time",
      description: "ISO 8601 timestamp of workflow creation"
    },
    lastModified: {
      type: "string",
      format: "date-time",
      description: "ISO 8601 timestamp of last modification"
    },
    version: {
      type: "string",
      description: "Workflow schema version",
      default: "1.0.0"
    }
  },
  definitions: {
    node: {
      type: "object",
      required: ["id", "type", "position"],
      properties: {
        id: {
          type: "string",
          description: "Unique node identifier"
        },
        type: {
          type: "string",
          description: "Node type identifier",
          enum: [
            "start",
            "aiAgent",
            "openaiChatModel",
            "anthropicChatModel",
            "googleChatModel",
            "gmaps_create",
            "gmaps_locations",
            "gmaps_nearby_places",
            "whatsappSendMessage",
            "whatsappReceiveMessage"
          ]
        },
        position: {
          type: "object",
          required: ["x", "y"],
          properties: {
            x: {
              type: "number",
              description: "X coordinate on canvas"
            },
            y: {
              type: "number",
              description: "Y coordinate on canvas"
            }
          }
        },
        data: {
          type: "object",
          description: "Node-specific parameters and configuration",
          properties: {
            label: {
              type: "string",
              description: "Display label for the node"
            }
          },
          additionalProperties: true
        },
        selected: {
          type: "boolean",
          description: "Whether node is currently selected"
        },
        dragging: {
          type: "boolean",
          description: "Whether node is being dragged"
        }
      },
      additionalProperties: true
    },
    edge: {
      type: "object",
      required: ["id", "source", "target"],
      properties: {
        id: {
          type: "string",
          description: "Unique edge identifier"
        },
        source: {
          type: "string",
          description: "Source node ID"
        },
        target: {
          type: "string",
          description: "Target node ID"
        },
        sourceHandle: {
          type: "string",
          description: "Source handle identifier",
          default: "output-main"
        },
        targetHandle: {
          type: "string",
          description: "Target handle identifier",
          default: "input-main"
        },
        type: {
          type: "string",
          description: "Edge rendering type",
          enum: ["default", "straight", "step", "smoothstep", "simplebezier"],
          default: "default"
        },
        animated: {
          type: "boolean",
          description: "Whether edge is animated"
        },
        style: {
          type: "object",
          description: "Custom edge styling"
        },
        label: {
          type: "string",
          description: "Edge label text"
        }
      },
      additionalProperties: true
    }
  }
};

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
