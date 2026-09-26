package executor

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	authorizationv1 "k8s.io/api/authorization/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	"k8s.io/client-go/tools/remotecommand"
	utilexec "k8s.io/client-go/util/exec"

	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/config"
	"github.com/onyx-dot-app/python-sandbox/code-interpreter-go/internal/logging"
)

const (
	podDeleteRetries               = 3
	podDeleteRetryDelay            = 200 * time.Millisecond
	podDeleteConfirmTimeout        = 2 * time.Second
	podReadyTimeout                = 30 * time.Second
	sessionLabelSelector           = "app=" + SessionAppLabel + ",component=" + SessionComponentLabel
	executorUID             int64  = 65532
	executorContainerName   string = "executor"
)

// KubernetesExecutor runs code inside isolated Kubernetes pods. It is the Go
// port of app/services/executor_kubernetes.py.
type KubernetesExecutor struct {
	clientset        kubernetes.Interface
	restConfig       *rest.Config
	namespace        string
	image            string
	serviceAccount   string
	netAdminLockdown bool
	log              *slog.Logger
}

// NewKubernetesExecutor loads in-cluster configuration, falling back to the
// local kubeconfig, and returns a ready executor.
func NewKubernetesExecutor(cfg config.Config) (*KubernetesExecutor, error) {
	restConfig, err := rest.InClusterConfig()
	if err != nil {
		restConfig, err = clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
			clientcmd.NewDefaultClientConfigLoadingRules(),
			&clientcmd.ConfigOverrides{},
		).ClientConfig()
		if err != nil {
			return nil, fmt.Errorf("failed to load Kubernetes configuration: %w", err)
		}
	}

	clientset, err := kubernetes.NewForConfig(restConfig)
	if err != nil {
		return nil, fmt.Errorf("failed to create Kubernetes client: %w", err)
	}

	return &KubernetesExecutor{
		clientset:        clientset,
		restConfig:       restConfig,
		namespace:        cfg.KubernetesNamespace,
		image:            cfg.KubernetesImage,
		serviceAccount:   cfg.KubernetesServiceAccount,
		netAdminLockdown: cfg.KubernetesNetAdminLockdown,
		log:              logging.Named("executor.kubernetes"),
	}, nil
}

// CheckHealth verifies the Kubernetes API is reachable and we can create pods
// in the namespace.
func (k *KubernetesExecutor) CheckHealth() HealthCheck {
	review, err := k.clientset.AuthorizationV1().SelfSubjectAccessReviews().Create(
		context.Background(),
		&authorizationv1.SelfSubjectAccessReview{
			Spec: authorizationv1.SelfSubjectAccessReviewSpec{
				ResourceAttributes: &authorizationv1.ResourceAttributes{
					Namespace: k.namespace,
					Verb:      "create",
					Resource:  "pods",
				},
			},
		},
		metav1.CreateOptions{},
	)
	if err != nil {
		var statusErr *apierrors.StatusError
		if errors.As(err, &statusErr) {
			return HealthCheck{
				Status: "error",
				Message: fmt.Sprintf(
					"Kubernetes API error (namespace=%s): %s",
					k.namespace, statusErr.ErrStatus.Reason,
				),
			}
		}
		return HealthCheck{
			Status:  "error",
			Message: fmt.Sprintf("Kubernetes API not reachable: %v", err),
		}
	}
	if !review.Status.Allowed {
		reason := review.Status.Reason
		if reason == "" {
			reason = "no reason provided"
		}
		k.log.Warn(fmt.Sprintf(
			"Health check failed: cannot create pods in namespace=%s (reason=%s)",
			k.namespace, reason,
		))
		return HealthCheck{
			Status: "error",
			Message: fmt.Sprintf(
				"Service account lacks permission to create pods in namespace=%s", k.namespace,
			),
		}
	}
	return HealthCheck{Status: "ok"}
}

// createPodManifest builds a pod manifest for an isolated executor container.
//
// command is the executor container's command (e.g. ["sleep", "3600"]).
// activeDeadlineSeconds, when set, instructs kubelet to stop the pod at that
// age — used by sessions to enforce TTL even if the API is down.
func (k *KubernetesExecutor) createPodManifest(
	podName string,
	command []string,
	labels map[string]string,
	annotations map[string]string,
	activeDeadlineSeconds *int64,
	memoryLimitMB *int,
	cpuTimeLimitSec *int,
) *corev1.Pod {
	limits := corev1.ResourceList{}
	requests := corev1.ResourceList{}

	if memoryLimitMB != nil {
		memoryLimit := max(*memoryLimitMB, 16)
		limits[corev1.ResourceMemory] = resource.MustParse(fmt.Sprintf("%dMi", memoryLimit))
		requests[corev1.ResourceMemory] = resource.MustParse(
			fmt.Sprintf("%dMi", min(memoryLimit, 64)),
		)
	}
	if cpuTimeLimitSec != nil {
		cpuLimit := max(*cpuTimeLimitSec, 1)
		limits[corev1.ResourceCPU] = resource.MustParse(strconv.Itoa(cpuLimit))
		requests[corev1.ResourceCPU] = resource.MustParse("100m")
	}

	var resources corev1.ResourceRequirements
	if len(limits) > 0 {
		resources = corev1.ResourceRequirements{Limits: limits, Requests: requests}
	}

	uid := executorUID
	falseVal := false
	trueVal := true

	container := corev1.Container{
		Name:       executorContainerName,
		Image:      k.image,
		Command:    command,
		WorkingDir: "/workspace",
		Resources:  resources,
		SecurityContext: &corev1.SecurityContext{
			RunAsUser:                &uid,
			RunAsGroup:               &uid,
			AllowPrivilegeEscalation: &falseVal,
			ReadOnlyRootFilesystem:   &falseVal,
			Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
		Env: []corev1.EnvVar{
			{Name: "PYTHONUNBUFFERED", Value: "1"},
			{Name: "PYTHONDONTWRITEBYTECODE", Value: "1"},
			{Name: "PYTHONIOENCODING", Value: "utf-8"},
			{Name: "MPLCONFIGDIR", Value: "/tmp/matplotlib"},
		},
		VolumeMounts: []corev1.VolumeMount{
			{Name: "workspace", MountPath: "/workspace"},
			{Name: "tmp", MountPath: "/tmp"},
		},
	}

	// Use iptables in an init container to drop all outbound traffic before
	// the main executor container starts. Since all containers in a pod share
	// a network namespace, rules set here apply to the executor container as
	// well. This eliminates the race condition where the pod can send network
	// requests before the Kubernetes NetworkPolicy is enforced by the CNI.
	//
	// This requires the NET_ADMIN capability. Environments whose CNI enforces
	// NetworkPolicies without that race (or that disallow NET_ADMIN) can
	// disable this and rely on a NetworkPolicy instead.
	var initContainers []corev1.Container
	if k.netAdminLockdown {
		rootUID := int64(0)
		iptablesScript := "set -e && iptables -A OUTPUT -j DROP && ip6tables -A OUTPUT -j DROP"
		initContainers = append(initContainers, corev1.Container{
			Name:    "network-lockdown",
			Image:   k.image,
			Command: []string{"sh", "-c", iptablesScript},
			SecurityContext: &corev1.SecurityContext{
				RunAsUser:                &rootUID,
				RunAsNonRoot:             &falseVal,
				AllowPrivilegeEscalation: &falseVal,
				ReadOnlyRootFilesystem:   &trueVal,
				Capabilities: &corev1.Capabilities{
					Drop: []corev1.Capability{"ALL"},
					Add:  []corev1.Capability{"NET_ADMIN"},
				},
			},
			Resources: corev1.ResourceRequirements{
				Limits: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("100m"),
					corev1.ResourceMemory: resource.MustParse("32Mi"),
				},
				Requests: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("10m"),
					corev1.ResourceMemory: resource.MustParse("16Mi"),
				},
			},
		})
	}

	workspaceSize := resource.MustParse("100Mi")
	tmpSize := resource.MustParse("64Mi")
	fsGroup := executorUID

	spec := corev1.PodSpec{
		InitContainers:        initContainers,
		Containers:            []corev1.Container{container},
		RestartPolicy:         corev1.RestartPolicyNever,
		ActiveDeadlineSeconds: activeDeadlineSeconds,
		ServiceAccountName:    k.serviceAccount,
		Volumes: []corev1.Volume{
			{
				Name: "workspace",
				VolumeSource: corev1.VolumeSource{
					EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: &workspaceSize},
				},
			},
			{
				Name: "tmp",
				VolumeSource: corev1.VolumeSource{
					EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: &tmpSize},
				},
			},
		},
		SecurityContext: &corev1.PodSecurityContext{
			RunAsNonRoot: &trueVal,
			FSGroup:      &fsGroup,
		},
	}

	return &corev1.Pod{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Pod"},
		ObjectMeta: metav1.ObjectMeta{
			Name:        podName,
			Namespace:   k.namespace,
			Labels:      labels,
			Annotations: annotations,
		},
		Spec: spec,
	}
}

// waitForPodReady polls until the pod reaches the Running phase.
func (k *KubernetesExecutor) waitForPodReady(podName string, timeout time.Duration) error {
	k.log.Info("Waiting for pod " + podName + " to be ready")
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		pod, err := k.clientset.CoreV1().Pods(k.namespace).Get(
			context.Background(), podName, metav1.GetOptions{},
		)
		if err != nil {
			return err
		}
		if pod.Status.Phase == corev1.PodRunning {
			k.log.Info("Pod " + podName + " is running")
			return nil
		}
		time.Sleep(100 * time.Millisecond)
	}
	return fmt.Errorf(
		"Pod %s did not become ready in %d seconds", podName, int(timeout.Seconds()),
	)
}

// podExec runs a command in the executor container over SPDY. Streams are
// wired directly; a nil reader/writer disables that stream. The returned
// error is nil on exit code 0, a *utilexec.CodeExitError on non-zero exit,
// or the transport/context error otherwise.
func (k *KubernetesExecutor) podExec(
	ctx context.Context,
	podName string,
	command []string,
	stdin io.Reader,
	stdout io.Writer,
	stderr io.Writer,
) error {
	req := k.clientset.CoreV1().RESTClient().Post().
		Resource("pods").
		Name(podName).
		Namespace(k.namespace).
		SubResource("exec").
		VersionedParams(&corev1.PodExecOptions{
			Container: executorContainerName,
			Command:   command,
			Stdin:     stdin != nil,
			Stdout:    stdout != nil,
			Stderr:    stderr != nil,
			TTY:       false,
		}, scheme.ParameterCodec)

	executor, err := remotecommand.NewSPDYExecutor(k.restConfig, "POST", req.URL())
	if err != nil {
		return err
	}
	return executor.StreamWithContext(ctx, remotecommand.StreamOptions{
		Stdin:  stdin,
		Stdout: stdout,
		Stderr: stderr,
	})
}

// exitCodeFromExecError translates a podExec error into an exit code:
// nil → 0, CodeExitError → its code, anything else → nil (unknown).
func exitCodeFromExecError(err error) *int {
	if err == nil {
		zero := 0
		return &zero
	}
	var codeErr utilexec.CodeExitError
	if errors.As(err, &codeErr) {
		code := codeErr.Code
		return &code
	}
	return nil
}

// uploadTarToPod uploads and extracts a tar archive into the pod's workspace.
func (k *KubernetesExecutor) uploadTarToPod(podName string, tarArchive []byte) error {
	k.log.Info(fmt.Sprintf(
		"Uploading tar archive (%d bytes) to pod %s", len(tarArchive), podName,
	))
	var stderr bytes.Buffer
	err := k.podExec(
		context.Background(),
		podName,
		[]string{"tar", "-x", "-C", "/workspace"},
		bytes.NewReader(tarArchive),
		io.Discard,
		&stderr,
	)
	if err != nil {
		return fmt.Errorf(
			"Tar extraction failed: %v. stderr: %s",
			err, strings.ToValidUTF8(stderr.String(), "�"),
		)
	}
	k.log.Info("Tar extraction completed for pod " + podName)
	return nil
}

// killProcessesInPod best-effort SIGKILLs all processes with the given name
// in the pod.
func (k *KubernetesExecutor) killProcessesInPod(podName, processName string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := k.podExec(
		ctx, podName, []string{"pkill", "-9", processName}, nil, io.Discard, io.Discard,
	); err != nil {
		var codeErr utilexec.CodeExitError
		if !errors.As(err, &codeErr) {
			k.log.Warn(fmt.Sprintf(
				"Failed to kill %s process in pod %s: %v", processName, podName, err,
			))
		}
	}
}

// extractWorkspaceSnapshot extracts files from the pod workspace after
// execution using tar. SPDY streams are binary-safe, so the tar bytes are
// captured directly. Failures yield an empty snapshot, never an error.
func (k *KubernetesExecutor) extractWorkspaceSnapshot(podName string) []WorkspaceEntry {
	var stdout, stderr bytes.Buffer
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	k.log.Info("Starting tar extraction from pod " + podName)
	err := k.podExec(
		ctx,
		podName,
		[]string{"tar", "-c", "--exclude=__main__.py", "-C", "/workspace", "."},
		nil,
		&stdout,
		&stderr,
	)
	if err != nil {
		k.log.Error(fmt.Sprintf(
			"Failed to extract workspace snapshot from pod %s: %v (stderr: %s)",
			podName, err, stderr.String(),
		))
		return nil
	}

	entries, err := ParseWorkspaceSnapshot(stdout.Bytes())
	if err != nil {
		k.log.Error(fmt.Sprintf("Failed to parse workspace snapshot: %v", err))
		return nil
	}
	k.log.Info(fmt.Sprintf("Extracted %d workspace entries", len(entries)))
	return entries
}

// waitForPodDeleted polls until the pod is gone, returning true on success.
func (k *KubernetesExecutor) waitForPodDeleted(podName string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		_, err := k.clientset.CoreV1().Pods(k.namespace).Get(
			context.Background(), podName, metav1.GetOptions{},
		)
		if err != nil {
			if apierrors.IsNotFound(err) {
				return true
			}
			k.log.Warn(fmt.Sprintf(
				"Error while checking pod deletion for %s in namespace %s: %v",
				podName, k.namespace, err,
			))
			return false
		}
		time.Sleep(100 * time.Millisecond)
	}
	return false
}

// cleanupPod deletes a pod with retries and logs any cleanup failures.
func (k *KubernetesExecutor) cleanupPod(podName string) {
	gracePeriod := int64(0)
	deleteOpts := metav1.DeleteOptions{GracePeriodSeconds: &gracePeriod}

	for attempt := 1; attempt <= podDeleteRetries; attempt++ {
		err := k.clientset.CoreV1().Pods(k.namespace).Delete(
			context.Background(), podName, deleteOpts,
		)
		if err != nil {
			if apierrors.IsNotFound(err) {
				return
			}
			k.log.Warn(fmt.Sprintf(
				"Failed to delete pod %s in namespace %s on attempt %d/%d: %v",
				podName, k.namespace, attempt, podDeleteRetries, err,
			))
		} else {
			if k.waitForPodDeleted(podName, podDeleteConfirmTimeout) {
				return
			}
			k.log.Warn(fmt.Sprintf(
				"Pod %s still exists after delete request on attempt %d/%d",
				podName, attempt, podDeleteRetries,
			))
		}

		if attempt < podDeleteRetries {
			time.Sleep(podDeleteRetryDelay * time.Duration(attempt))
		}
	}

	k.log.Error(fmt.Sprintf(
		"Failed to confirm deletion of pod %s in namespace %s after %d attempts",
		podName, k.namespace, podDeleteRetries,
	))
}

// preparePod creates an ephemeral executor pod, waits for it to run, and
// stages the code + files. The returned cleanup function deletes the pod.
func (k *KubernetesExecutor) preparePod(opts ExecOptions) (string, func(), error) {
	podName := "code-exec-" + strings.ReplaceAll(uuid.NewString(), "-", "")
	k.log.Info("Starting execution in pod " + podName)

	manifest := k.createPodManifest(
		podName,
		[]string{"sleep", "3600"},
		map[string]string{"app": "code-interpreter", "component": "executor"},
		nil,
		nil,
		opts.MemoryLimitMB,
		opts.CPUTimeLimitSec,
	)

	cleanup := func() {
		k.log.Info("Cleaning up pod " + podName)
		k.cleanupPod(podName)
	}

	k.log.Info(fmt.Sprintf("Creating pod %s in namespace %s", podName, k.namespace))
	if _, err := k.clientset.CoreV1().Pods(k.namespace).Create(
		context.Background(), manifest, metav1.CreateOptions{},
	); err != nil {
		return "", func() {}, fmt.Errorf("failed to create pod %s: %w", podName, err)
	}

	if err := k.waitForPodReady(podName, podReadyTimeout); err != nil {
		cleanup()
		return "", func() {}, err
	}

	owner := &TarOwner{UID: int(executorUID), GID: int(executorUID)}
	tarArchive, err := CreateTarArchive(&opts.Code, opts.Files, opts.LastLineInteractive, owner)
	if err != nil {
		cleanup()
		return "", func() {}, err
	}
	if err := k.uploadTarToPod(podName, tarArchive); err != nil {
		cleanup()
		return "", func() {}, err
	}

	return podName, cleanup, nil
}

// ExecutePython executes Python code inside a Kubernetes pod.
func (k *KubernetesExecutor) ExecutePython(opts ExecOptions) (ExecutionResult, error) {
	podName, cleanup, err := k.preparePod(opts)
	if err != nil {
		return ExecutionResult{}, err
	}
	defer cleanup()

	k.log.Info("Executing Python code in pod " + podName)
	start := time.Now()

	var stdin io.Reader
	if opts.Stdin != nil {
		stdin = strings.NewReader(*opts.Stdin)
	}
	var stdoutBuf, stderrBuf bytes.Buffer

	ctx, cancel := context.WithTimeout(
		context.Background(), time.Duration(opts.TimeoutMs)*time.Millisecond,
	)
	defer cancel()

	execErr := k.podExec(
		ctx, podName, []string{"python", "/workspace/__main__.py"},
		stdin, &stdoutBuf, &stderrBuf,
	)

	timedOut := ctx.Err() == context.DeadlineExceeded
	if timedOut {
		k.killProcessesInPod(podName, "python")
	} else if execErr != nil && exitCodeFromExecError(execErr) == nil {
		return ExecutionResult{}, fmt.Errorf(
			"error during execution in pod %s: %w", podName, execErr,
		)
	}

	exitCode := exitCodeFromExecError(execErr)
	k.log.Info(fmt.Sprintf(
		"Python execution completed. Exit code: %v, Timed out: %v", exitCode, timedOut,
	))

	k.log.Info("Extracting workspace snapshot from pod " + podName)
	workspaceSnapshot := k.extractWorkspaceSnapshot(podName)

	durationMs := time.Since(start).Milliseconds()
	if timedOut {
		exitCode = nil
	}

	k.log.Info(fmt.Sprintf("Execution completed in %dms", durationMs))
	return ExecutionResult{
		Stdout:     TruncateOutput(stdoutBuf.Bytes(), opts.MaxOutputBytes),
		Stderr:     TruncateOutput(stderrBuf.Bytes(), opts.MaxOutputBytes),
		ExitCode:   exitCode,
		TimedOut:   timedOut,
		DurationMs: durationMs,
		Files:      workspaceSnapshot,
	}, nil
}

// ExecutePythonStreaming executes Python code, emitting output chunks as they
// arrive, and returns the final result.
func (k *KubernetesExecutor) ExecutePythonStreaming(
	opts ExecOptions,
	emit EmitFunc,
) (StreamResult, error) {
	podName, cleanup, err := k.preparePod(opts)
	if err != nil {
		return StreamResult{}, err
	}
	defer cleanup()

	start := time.Now()

	var stdin io.Reader
	if opts.Stdin != nil {
		stdin = strings.NewReader(*opts.Stdin)
	}

	var mu sync.Mutex
	stdoutWriter := &trackerWriter{
		tracker: newStreamTracker("stdout", opts.MaxOutputBytes), emit: emit, mu: &mu,
	}
	stderrWriter := &trackerWriter{
		tracker: newStreamTracker("stderr", opts.MaxOutputBytes), emit: emit, mu: &mu,
	}

	ctx, cancel := context.WithTimeout(
		context.Background(), time.Duration(opts.TimeoutMs)*time.Millisecond,
	)
	defer cancel()

	execErr := k.podExec(
		ctx, podName, []string{"python", "/workspace/__main__.py"},
		stdin, stdoutWriter, stderrWriter,
	)

	timedOut := ctx.Err() == context.DeadlineExceeded
	if timedOut {
		k.killProcessesInPod(podName, "python")
	} else if execErr != nil && exitCodeFromExecError(execErr) == nil {
		return StreamResult{}, fmt.Errorf(
			"error during execution in pod %s: %w", podName, execErr,
		)
	}

	for _, w := range []*trackerWriter{stdoutWriter, stderrWriter} {
		mu.Lock()
		if chunk := w.tracker.flush(); chunk != nil {
			emit(*chunk)
		}
		mu.Unlock()
	}

	exitCode := exitCodeFromExecError(execErr)
	workspaceSnapshot := k.extractWorkspaceSnapshot(podName)
	durationMs := time.Since(start).Milliseconds()
	if timedOut {
		exitCode = nil
	}

	return StreamResult{
		ExitCode:   exitCode,
		TimedOut:   timedOut,
		DurationMs: durationMs,
		Files:      workspaceSnapshot,
	}, nil
}

// CreateSession creates a long-lived session pod. activeDeadlineSeconds and
// the idle `sleep` both enforce the TTL even if this process crashes.
func (k *KubernetesExecutor) CreateSession(
	ttlSeconds int,
	files []StagedFile,
	cpuTimeLimitSec *int,
	memoryLimitMB *int,
) (SessionInfo, error) {
	podName := SessionNamePrefix + strings.ReplaceAll(uuid.NewString(), "-", "")
	expiresAt := float64(time.Now().UnixNano())/1e9 + float64(ttlSeconds)
	deadline := int64(ttlSeconds)

	manifest := k.createPodManifest(
		podName,
		[]string{"sleep", strconv.Itoa(ttlSeconds)},
		map[string]string{"app": SessionAppLabel, "component": SessionComponentLabel},
		map[string]string{
			SessionExpiresAtKey: strconv.FormatFloat(expiresAt, 'f', -1, 64),
		},
		&deadline,
		memoryLimitMB,
		cpuTimeLimitSec,
	)

	k.log.Info(fmt.Sprintf(
		"Creating session pod %s in namespace %s (ttl=%ds)", podName, k.namespace, ttlSeconds,
	))
	if _, err := k.clientset.CoreV1().Pods(k.namespace).Create(
		context.Background(), manifest, metav1.CreateOptions{},
	); err != nil {
		return SessionInfo{}, fmt.Errorf("failed to create session pod %s: %w", podName, err)
	}

	if err := k.waitForPodReady(podName, podReadyTimeout); err != nil {
		k.cleanupPod(podName)
		return SessionInfo{}, err
	}
	if len(files) > 0 {
		owner := &TarOwner{UID: int(executorUID), GID: int(executorUID)}
		tarArchive, err := CreateTarArchive(nil, files, true, owner)
		if err != nil {
			k.cleanupPod(podName)
			return SessionInfo{}, err
		}
		if err := k.uploadTarToPod(podName, tarArchive); err != nil {
			k.cleanupPod(podName)
			return SessionInfo{}, err
		}
	}

	return SessionInfo{SessionID: podName, ExpiresAt: expiresAt}, nil
}

// DeleteSession deletes a session pod by ID. Returns false when it does not
// exist.
func (k *KubernetesExecutor) DeleteSession(sessionID string) (bool, error) {
	if !strings.HasPrefix(sessionID, SessionNamePrefix) {
		return false, nil
	}
	gracePeriod := int64(0)
	err := k.clientset.CoreV1().Pods(k.namespace).Delete(
		context.Background(), sessionID, metav1.DeleteOptions{GracePeriodSeconds: &gracePeriod},
	)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

// ReapExpiredSessions deletes session pods whose expires-at annotation has
// elapsed. Returns the number reaped.
func (k *KubernetesExecutor) ReapExpiredSessions() (int, error) {
	pods, err := k.clientset.CoreV1().Pods(k.namespace).List(
		context.Background(), metav1.ListOptions{LabelSelector: sessionLabelSelector},
	)
	if err != nil {
		k.log.Warn(fmt.Sprintf("Failed to list session pods for reap: %v", err))
		return 0, nil
	}

	now := float64(time.Now().UnixNano()) / 1e9
	gracePeriod := int64(0)
	reaped := 0
	for _, pod := range pods.Items {
		expiresStr, ok := pod.Annotations[SessionExpiresAtKey]
		if !ok {
			continue
		}
		expiresAt, err := strconv.ParseFloat(expiresStr, 64)
		if err != nil {
			k.log.Warn(fmt.Sprintf(
				"Session pod %s has invalid expires-at annotation %q", pod.Name, expiresStr,
			))
			continue
		}
		if expiresAt >= now {
			continue
		}
		err = k.clientset.CoreV1().Pods(k.namespace).Delete(
			context.Background(), pod.Name,
			metav1.DeleteOptions{GracePeriodSeconds: &gracePeriod},
		)
		if err != nil {
			if apierrors.IsNotFound(err) {
				continue
			}
			k.log.Warn(fmt.Sprintf("Failed to reap session pod %s: %v", pod.Name, err))
			continue
		}
		reaped++
		k.log.Info("Reaped expired session pod " + pod.Name)
	}
	return reaped, nil
}

// ExecuteBashInSession runs a bash command inside an existing session pod.
//
// Network restrictions established at pod creation (the iptables init
// container) remain in force — exec inherits the pod's network namespace.
func (k *KubernetesExecutor) ExecuteBashInSession(
	sessionID string,
	cmd string,
	timeoutMs int,
	maxOutputBytes int,
) (ExecutionResult, error) {
	if !strings.HasPrefix(sessionID, SessionNamePrefix) {
		return ExecutionResult{}, &SessionNotFoundError{SessionID: sessionID}
	}

	if _, err := k.clientset.CoreV1().Pods(k.namespace).Get(
		context.Background(), sessionID, metav1.GetOptions{},
	); err != nil {
		if apierrors.IsNotFound(err) {
			return ExecutionResult{}, &SessionNotFoundError{SessionID: sessionID}
		}
		return ExecutionResult{}, err
	}

	start := time.Now()
	var stdoutBuf, stderrBuf bytes.Buffer

	ctx, cancel := context.WithTimeout(
		context.Background(), time.Duration(timeoutMs)*time.Millisecond,
	)
	defer cancel()

	execErr := k.podExec(
		ctx, sessionID, []string{"bash", "-c", cmd}, nil, &stdoutBuf, &stderrBuf,
	)

	timedOut := ctx.Err() == context.DeadlineExceeded
	if timedOut {
		k.killProcessesInPod(sessionID, "bash")
	} else if execErr != nil && exitCodeFromExecError(execErr) == nil {
		return ExecutionResult{}, fmt.Errorf(
			"error during bash execution in session %s: %w", sessionID, execErr,
		)
	}

	exitCode := exitCodeFromExecError(execErr)
	if timedOut {
		exitCode = nil
	}
	durationMs := time.Since(start).Milliseconds()

	return ExecutionResult{
		Stdout:     TruncateOutput(stdoutBuf.Bytes(), maxOutputBytes),
		Stderr:     TruncateOutput(stderrBuf.Bytes(), maxOutputBytes),
		ExitCode:   exitCode,
		TimedOut:   timedOut,
		DurationMs: durationMs,
	}, nil
}
