# Database schema (ERD)

Auto-generated from the SQLModel metadata by `make erddump` — **do not edit by
hand**. Regenerate after any model change and commit the result in the same diff
(the pre-push hook does this for you; CI fails on drift).

```mermaid
erDiagram
    ai_models {
        parameters JSONB
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        name VARCHAR(255)
        description TEXT
        model_alias VARCHAR(128)
        provider providervendor
        input_modalities VARCHAR[]
        output_modalities VARCHAR[]
        provider_model_id VARCHAR(255)
        endpoint_name VARCHAR(255)
        inference_endpoint VARCHAR(1024)
        warmup_enabled BOOLEAN
        advanced_params_disabled BOOLEAN
        icon_file VARCHAR(255)
        labels VARCHAR[]
        extras JSONB
        api_key_encrypted VARCHAR(1024)
        disabled_at TIMESTAMP_WITH_TIME_ZONE
        health_check_status healthcheckstatus
        last_health_check_at TIMESTAMP_WITH_TIME_ZONE
        last_health_reason VARCHAR(64)
        capability_mismatch VARCHAR(255)
        last_healthy_at TIMESTAMP_WITH_TIME_ZONE
        inactivity_alert_hours INTEGER
        last_used_at TIMESTAMP_WITH_TIME_ZONE
        last_warmup_at TIMESTAMP_WITH_TIME_ZONE
        inactivity_alerted_at TIMESTAMP_WITH_TIME_ZONE
    }
    audit_logs {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        actor_id UUID
        actor_email VARCHAR(320)
        action VARCHAR(64)
        object_type VARCHAR(64)
        object_id UUID
        before JSONB
        after JSONB
        context JSONB
        request_id VARCHAR(64)
    }
    organizations {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        name VARCHAR(255)
        description VARCHAR(1024)
    }
    roles {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        name VARCHAR(64)
        display_name VARCHAR(128)
        description VARCHAR(255)
        permissions JSONB
        is_system BOOLEAN
        is_active BOOLEAN
        is_default BOOLEAN
        is_participant_default BOOLEAN
        is_object_assignable BOOLEAN
    }
    terms_documents {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        version VARCHAR(64)
        content TEXT
        published_at TIMESTAMP_WITH_TIME_ZONE
    }
    users {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        email VARCHAR(320)
        email_verified_at TIMESTAMP_WITH_TIME_ZONE
        first_name VARCHAR(255)
        last_name VARCHAR(255)
        password VARCHAR(255)
        password_cleared_at TIMESTAMP_WITH_TIME_ZONE
        organization_id UUID FK
        status userstatus
        accepted_terms_id UUID FK
        terms_accepted_at TIMESTAMP_WITH_TIME_ZONE
        consent_emails BOOLEAN
    }
    annotation_labels {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        created_by_id UUID FK
        key VARCHAR(64)
        name VARCHAR(128)
    }
    data_licenses {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        name VARCHAR(255)
        version VARCHAR(64)
        short_description TEXT
        content TEXT
        reference_url VARCHAR(1024)
        protects_conversation_data BOOLEAN
        spdx_id VARCHAR(64)
        created_by_id UUID FK
    }
    email_verifications {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        user_id UUID FK
        token_hash VARCHAR(64)
        expires_at TIMESTAMP_WITH_TIME_ZONE
        revoked_at TIMESTAMP_WITH_TIME_ZONE
        verified_at TIMESTAMP_WITH_TIME_ZONE
        status emailverificationstatus
    }
    invitations {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        user_id UUID FK
        token_hash VARCHAR(64)
        expires_at TIMESTAMP_WITH_TIME_ZONE
        revoked_at TIMESTAMP_WITH_TIME_ZONE
        invited_by_user_id UUID FK
        accepted_at TIMESTAMP_WITH_TIME_ZONE
        status invitationstatus
        object_type objecttype
        object_id UUID
    }
    media_assets {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        key VARCHAR(1024)
        content_type VARCHAR(128)
        size_bytes INTEGER
        width INTEGER
        height INTEGER
        is_private BOOLEAN
        created_by_id UUID FK
    }
    notifications {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        user_id UUID FK
        name VARCHAR(255)
        description TEXT
        read_at TIMESTAMP_WITH_TIME_ZONE
        object_type VARCHAR(64)
        object_id UUID
    }
    object_role_assignments {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        object_type objecttype
        object_id UUID
        user_id UUID FK
        role_id UUID FK
    }
    outbound_emails {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        template_name VARCHAR(64)
        recipient VARCHAR(320)
        context JSONB
        backend VARCHAR(32)
        subject VARCHAR(998)
        status outboundemailstatus
        requested_by_user_id UUID FK
        batch_key UUID
        error_type VARCHAR(128)
        error_message VARCHAR(2000)
        attempts INTEGER
        sent_at TIMESTAMP_WITH_TIME_ZONE
        celery_task_id VARCHAR(64)
        provider_message_id VARCHAR(255)
    }
    password_reset_tokens {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        user_id UUID FK
        token_hash VARCHAR(64)
        expires_at TIMESTAMP_WITH_TIME_ZONE
        used_at TIMESTAMP_WITH_TIME_ZONE
        revoked_at TIMESTAMP_WITH_TIME_ZONE
        status passwordresettokenstatus
    }
    provider_identities {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        provider VARCHAR(64)
        subject VARCHAR(255)
        user_id UUID FK
    }
    saved_views {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        created_by_id UUID FK
        resource VARCHAR(64)
        name VARCHAR(128)
        state JSONB
    }
    user_roles {
        user_id UUID PK,FK
        role_id UUID PK,FK
    }
    evaluation_groups {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        title VARCHAR(255)
        description TEXT
        created_by_id UUID FK
        status publicationstatus
        rejection_reason TEXT
        access_level evaluationgroupaccesslevel
        metrics_access_during metricsaccesslevel
        metrics_access_after metricsaccesslevel
        organization_id UUID FK
        start_date DATE
        end_date DATE
        data_license_id UUID FK
    }
    platform_settings {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        default_license_id UUID FK
        invite_only BOOLEAN
        email_verification_ttl_hours INTEGER
        password_min_length INTEGER
        password_require_uppercase BOOLEAN
        password_require_digit BOOLEAN
        password_require_symbol BOOLEAN
        password_reset_cooldown_seconds INTEGER
        password_reset_max_per_day INTEGER
    }
    evaluation_group_ai_models {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        evaluation_group_id UUID FK
        model_id UUID FK
    }
    evaluations {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        title VARCHAR(255)
        description TEXT
        mask_models_enabled BOOLEAN
        tags_enabled BOOLEAN
        tags_restricted BOOLEAN
        cover_image VARCHAR(1024)
        data_license_id UUID FK
        status evaluationstatus
        rejection_reason TEXT
        created_by_id UUID FK
        evaluation_group_id UUID FK
    }
    evaluation_ai_models {
        parameters JSONB
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        model_id UUID FK
        evaluation_id UUID FK
        model_display_mask VARCHAR(255)
    }
    evaluation_tag_keys {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        evaluation_id UUID FK
        key VARCHAR(64)
    }
    export_jobs {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        template VARCHAR
        format VARCHAR
        evaluation_id UUID FK
        evaluation_group_id UUID FK
        requested_by_id UUID FK
        status exportjobstatus
        file_ref VARCHAR
        error TEXT
        expires_at TIMESTAMP_WITH_TIME_ZONE
        idempotency_key UUID
        filters JSONB
    }
    scenarios {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        name VARCHAR(255)
        description TEXT
        evaluation_id UUID FK
        position INTEGER
        required_reviews INTEGER
    }
    conversation_groups {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        user_id UUID FK
        evaluation_id UUID FK
        scenario_id UUID FK
        name VARCHAR(255)
    }
    tasks {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        name VARCHAR(255)
        description TEXT
        scenario_id UUID FK
    }
    conversations {
        parameters JSONB
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        user_id UUID FK
        evaluation_id UUID FK
        evaluation_ai_model_id UUID FK
        conversation_group_id UUID FK
        scenario_id UUID FK
        title VARCHAR(255)
        tags JSONB
        content_protected BOOLEAN
    }
    message_flags {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        reason TEXT
        red_flagged BOOLEAN
        comment TEXT
        status flagstatus
        created_by_id UUID FK
        conversation_id UUID FK
        evaluation_id UUID FK
        evaluation_group_id UUID FK
        scenario_id UUID FK
        task_id UUID FK
    }
    notes {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        text TEXT
        created_by_id UUID FK
        conversation_id UUID FK
        evaluation_id UUID FK
        evaluation_group_id UUID FK
    }
    task_completions {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        created_by_id UUID FK
        conversation_id UUID FK
        task_id UUID FK
        conversation_group_id UUID FK
        evaluation_id UUID FK
        evaluation_group_id UUID FK
        scenario_id UUID FK
    }
    turns {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        conversation_id UUID FK
        turn_index INTEGER
    }
    messages {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        turn_id UUID FK
        role messagerole
        status messagestatus
        content TEXT
        content_encrypted BOOLEAN
        slot VARCHAR(8)
        client_message_id UUID
        replaces_message_id UUID FK
        extra JSONB
        tags JSONB
    }
    reviews {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        message_flag_id UUID FK
        reviewer_id UUID FK
        assigned_by_id UUID FK
        evaluation_id UUID FK
        status reviewstatus
        successful_exploit BOOLEAN
        unique_exploit BOOLEAN
        valid_submission BOOLEAN
        number_prompts INTEGER
        notes TEXT
    }
    annotations {
        id UUID PK
        created_at TIMESTAMP_WITH_TIME_ZONE
        updated_at TIMESTAMP_WITH_TIME_ZONE
        deleted_at TIMESTAMP_WITH_TIME_ZONE
        deleted_by_id UUID
        message_id UUID FK
        label_id UUID FK
        created_by_id UUID FK
        conversation_id UUID FK
        evaluation_id UUID FK
        evaluation_group_id UUID FK
    }
    flagged_messages {
        message_flag_id UUID PK,FK
        message_id UUID PK,FK
    }
    message_images {
        message_id UUID PK,FK
        position INTEGER PK
        image_key VARCHAR(1024)
    }
    noted_messages {
        note_id UUID PK,FK
        message_id UUID PK,FK
    }

    organizations |o--o{ users : "organization_id"
    terms_documents |o--o{ users : "accepted_terms_id"
    users |o--o{ annotation_labels : "created_by_id"
    users |o--o{ data_licenses : "created_by_id"
    users ||--o{ email_verifications : "user_id"
    users |o--o{ invitations : "invited_by_user_id"
    users ||--o{ invitations : "user_id"
    users ||--o{ media_assets : "created_by_id"
    users ||--o{ notifications : "user_id"
    roles ||--o{ object_role_assignments : "role_id"
    users ||--o{ object_role_assignments : "user_id"
    users |o--o{ outbound_emails : "requested_by_user_id"
    users ||--o{ password_reset_tokens : "user_id"
    users ||--o{ provider_identities : "user_id"
    users ||--o{ saved_views : "created_by_id"
    roles ||--o{ user_roles : "role_id"
    users ||--o{ user_roles : "user_id"
    data_licenses |o--o{ evaluation_groups : "data_license_id"
    organizations |o--o{ evaluation_groups : "organization_id"
    users ||--o{ evaluation_groups : "created_by_id"
    data_licenses ||--o{ platform_settings : "default_license_id"
    ai_models ||--o{ evaluation_group_ai_models : "model_id"
    evaluation_groups ||--o{ evaluation_group_ai_models : "evaluation_group_id"
    data_licenses |o--o{ evaluations : "data_license_id"
    evaluation_groups ||--o{ evaluations : "evaluation_group_id"
    users ||--o{ evaluations : "created_by_id"
    ai_models ||--o{ evaluation_ai_models : "model_id"
    evaluations ||--o{ evaluation_ai_models : "evaluation_id"
    evaluations ||--o{ evaluation_tag_keys : "evaluation_id"
    evaluation_groups |o--o{ export_jobs : "evaluation_group_id"
    evaluations |o--o{ export_jobs : "evaluation_id"
    users ||--o{ export_jobs : "requested_by_id"
    evaluations ||--o{ scenarios : "evaluation_id"
    evaluations ||--o{ conversation_groups : "evaluation_id"
    scenarios ||--o{ conversation_groups : "scenario_id"
    users ||--o{ conversation_groups : "user_id"
    scenarios ||--o{ tasks : "scenario_id"
    conversation_groups ||--o{ conversations : "conversation_group_id"
    evaluation_ai_models ||--o{ conversations : "evaluation_ai_model_id"
    evaluations ||--o{ conversations : "evaluation_id"
    scenarios ||--o{ conversations : "scenario_id"
    users ||--o{ conversations : "user_id"
    conversations ||--o{ message_flags : "conversation_id"
    evaluation_groups ||--o{ message_flags : "evaluation_group_id"
    evaluations ||--o{ message_flags : "evaluation_id"
    scenarios |o--o{ message_flags : "scenario_id"
    tasks |o--o{ message_flags : "task_id"
    users ||--o{ message_flags : "created_by_id"
    conversations ||--o{ notes : "conversation_id"
    evaluation_groups ||--o{ notes : "evaluation_group_id"
    evaluations ||--o{ notes : "evaluation_id"
    users ||--o{ notes : "created_by_id"
    conversation_groups ||--o{ task_completions : "conversation_group_id"
    conversations ||--o{ task_completions : "conversation_id"
    evaluation_groups ||--o{ task_completions : "evaluation_group_id"
    evaluations ||--o{ task_completions : "evaluation_id"
    scenarios |o--o{ task_completions : "scenario_id"
    tasks ||--o{ task_completions : "task_id"
    users ||--o{ task_completions : "created_by_id"
    conversations ||--o{ turns : "conversation_id"
    messages |o--o{ messages : "replaces_message_id"
    turns ||--o{ messages : "turn_id"
    evaluations ||--o{ reviews : "evaluation_id"
    message_flags ||--o{ reviews : "message_flag_id"
    users ||--o{ reviews : "assigned_by_id"
    users ||--o{ reviews : "reviewer_id"
    annotation_labels ||--o{ annotations : "label_id"
    conversations ||--o{ annotations : "conversation_id"
    evaluation_groups ||--o{ annotations : "evaluation_group_id"
    evaluations ||--o{ annotations : "evaluation_id"
    messages ||--o{ annotations : "message_id"
    users ||--o{ annotations : "created_by_id"
    message_flags ||--o{ flagged_messages : "message_flag_id"
    messages ||--o{ flagged_messages : "message_id"
    messages ||--o{ message_images : "message_id"
    messages ||--o{ noted_messages : "message_id"
    notes ||--o{ noted_messages : "note_id"
```
