// Reads the image of an existing container app, so re-provisioning keeps the image that `azd deploy` pushed.
// A separate module avoids a circular dependency between the existing and the declared container app.

param exists bool
param name string

resource existingApp 'Microsoft.App/containerApps@2024-03-01' existing = if (exists) {
  name: name
}

output image string = exists ? existingApp!.properties.template.containers[0].image : ''
