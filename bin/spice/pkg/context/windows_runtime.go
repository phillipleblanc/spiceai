/*
Copyright 2024-2025 The Spice.ai OSS Authors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

     https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package context

import (
	"fmt"
	"log/slog"
	"strings"

	"github.com/spiceai/spiceai/bin/spice/pkg/constants"
	"github.com/spiceai/spiceai/bin/spice/pkg/util"
)

func (c *RuntimeContext) ensureWindowsRuntimeSupport(releaseTag string, flavor constants.Flavor) error {
	if !util.IsWindows() {
		return nil
	}

	image := windowsRuntimeDockerImage(releaseTag, flavor)

	if util.IsDockerAvailable() {
		slog.Info("Windows detected; pulling Spice runtime Docker image", "image", image)
		if err := util.PullDockerImage(image); err != nil {
			return fmt.Errorf("failed to pull Spice runtime Docker image %s: %w", image, err)
		}
		return fmt.Errorf("Spice runtime binaries are not supported on Windows. Pulled Docker image %s. Run it with Docker or install WSL2 to use the runtime.", image)
	}

	return fmt.Errorf("Spice runtime binaries are not supported on Windows. Install WSL2 and run `spice install` inside it.")
}

func windowsRuntimeDockerImage(releaseTag string, flavor constants.Flavor) string {
	version := strings.TrimPrefix(releaseTag, "v")
	if flavor == constants.FlavorAI {
		return fmt.Sprintf("spiceai/spiceai:%s-models", version)
	}

	return fmt.Sprintf("spiceai/spiceai:%s", version)
}
