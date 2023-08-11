from transformers import GPT2Model, GPT2PreTrainedModel, GPT2Tokenizer
import torch
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss, SmoothL1Loss
from transformer4planning.models.GPT2.models import *
from transformer4planning.models.encoders import *
from transformer4planning.models.decoders import *
from transformer4planning.models.encoder.mtr_encoder import MTREncoder

class GPTAutoRegressiveModelVector(GPT2PreTrainedModel):
    def forward(self, **kwargs):
        pass
    
    
class GPTNonAutoRegressiveModelVector(GPT2PreTrainedModel):
    def __init__(self, config, **kwargs):
        super().__init__(config)
        
        self.transformer = GPT2Model(config)
        model_args = kwargs["model_args"]
        self.model_args = model_args
        self.context_encoder = MTREncoder(kwargs["model_args"].vector_encoder_cfg.CONTEXT_ENCODER)
        
        self.predict_trajectory = model_args.predict_trajectory
        self.loss_fn = model_args.loss_fn
        self.ar_future_interval = model_args.ar_future_interval
        self.task = model_args.task
        self.action_m_embed = nn.Sequential(nn.Linear(4, config.n_embd), nn.Tanh())

        self.traj_decoder = None
        self.k = int(self.model_args.k)

        self.next_token_scorer_decoder = None
        self.key_points_decoder = None
        if self.predict_trajectory:
            out_features = 4 if model_args.predict_yaw else 2
            self.traj_decoder = DecoderResCat(config.n_inner, config.n_embd, out_features=out_features)
            if self.ar_future_interval > 0:
                self.key_points_decoder = DecoderResCat(config.n_inner, config.n_embd, out_features=out_features * self.k)
        if self.k > 1:
            self.next_token_scorer_decoder = DecoderResCat(config.n_inner, config.n_embd, out_features=self.k)

        self.clf_metrics = None

        # Initialize weights and apply final processing
        self.model_parallel = False
        self.device_map = None
        self.with_traffic_light = model_args.with_traffic_light
        
        if self.model_args.token_scenario_tag:
            self.tag_tokenizer = GPT2Tokenizer.from_pretrained(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gpt2-tokenizer'))
            self.tag_tokenizer.pad_token = self.tag_tokenizer.eos_token
            self.tag_embedding = nn.Embedding(self.tag_tokenizer.vocab_size, config.n_embd)
        self.post_init()

    @add_start_docstrings(PARALLELIZE_DOCSTRING)
    def parallelize(self, device_map=None):
        warnings.warn(
            "`GPT2LMHeadModel.parallelize` is deprecated and will be removed in v5 of Transformers, you should load"
            " your model with `device_map='balanced'` in the call to `from_pretrained`. You can also provide your own"
            " `device_map` but it needs to be a dictionary module_name to device, so for instance {'transformer.h.0':"
            " 0, 'transformer.h.1': 1, ...}",
            FutureWarning,
        )
        self.device_map = (
            get_device_map(len(self.transformer.h), range(torch.cuda.device_count()))
            if device_map is None
            else device_map
        )
        assert_device_map(self.device_map, len(self.transformer.h))
        self.transformer.parallelize(self.device_map)
        self.traj_decoder = self.traj_decoder.to(self.transformer.first_device)
        self.model_parallel = True

    @add_start_docstrings(DEPARALLELIZE_DOCSTRING)
    def deparallelize(self):
        warnings.warn(
            "Like `parallelize`, `deparallelize` is deprecated and will be removed in v5 of Transformers.",
            FutureWarning,
        )
        self.transformer.deparallelize()
        self.transformer = self.transformer.to("cpu")
        self.traj_decoder = self.traj_decoder.to("cpu")
        self.model_parallel = False
        torch.cuda.empty_cache()

    def _prepare_attention_mask_for_generation(self, input_embeds):
        return torch.ones(input_embeds.shape[:2], dtype=torch.long, device=input_embeds.device)

    def _prepare_position_ids_for_generation(self, attention_mask):
        position_ids = attention_mask.long().cumsum(-1) - 1
        position_ids.masked_fill_(attention_mask == 0, 1)
        return position_ids

    def forward(
            self,
            batch_size, input_dict, batch_sample_count,
            context_actions: Optional[torch.LongTensor] = None,
            past_key_values: Optional[Tuple[Tuple[torch.Tensor]]] = None,
            attention_mask: Optional[torch.FloatTensor] = None,
            token_type_ids: Optional[torch.LongTensor] = None,
            position_ids: Optional[torch.LongTensor] = None,
            head_mask: Optional[torch.FloatTensor] = None,
            encoder_hidden_states: Optional[torch.Tensor] = None,
            encoder_attention_mask: Optional[torch.FloatTensor] = None,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
            **kwargs
            ):
        
        batch_size = input_dict['obj_trajs'].shape[0]
        # encoder
        device = input_dict['obj_trajs'].device
        batch_dict = self.context_encoder({'input_dict': input_dict, 
                                           'batch_sample_count': batch_sample_count})
        
        obj_feature = batch_dict['obj_feature']
        map_feature = batch_dict['map_feature']
        state_embeds = torch.cat((map_feature, obj_feature), dim=1) # (bs, num_poly+num_obj, num_timestamp, 256)
        state_embeds = state_embeds.max(dim=1)[0]
        
        # traj
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        trajectory_label = input_dict['trajectory_label']
        pred_length = trajectory_label.shape[1]
        trajectory_label_mask = input_dict['center_gt_trajs_mask'].unsqueeze(-1)
        
        # action context
        context_actions = input_dict['center_objects_past']
        if self.model_args.x_random_walk > 0 and self.training:
            x_noise = torch.rand(context_actions.shape, device=device) * self.model_args.x_random_walk * 2 - self.model_args.x_random_walk
            context_actions[:, :, 0] += x_noise[:, :, 0]
        if self.model_args.y_random_walk > 0 and self.training:
            y_noise = torch.rand(context_actions.shape, device=device) * self.model_args.y_random_walk * 2 - self.model_args.y_random_walk
            context_actions[:, :, 1] += y_noise[:, :, 1]

        action_embeds = self.action_m_embed(context_actions)
        context_length = context_actions.shape[1]  # past_interval=10, past_frames=2 * 20, context_length = 40/10=4

        # create OAOAOA..
        n_embed = action_embeds.shape[-1]
        input_embeds = torch.zeros(
            (batch_size, context_length * 2, n_embed),
            dtype=torch.float32,
            device=device
        )
        input_embeds[:, ::2, :] = state_embeds  # index: 0, 2, 4, .., 18
        input_embeds[:, 1::2, :] = action_embeds  # index: 1, 3, 5, .., 19


        if self.model_args.token_scenario_tag:
            scenario_type = kwargs.get("scenario_type", None)
            scenario_tag_ids = list()
            for i in range(batch_size):
                scenario_tag_ids.append(torch.tensor(self.tag_tokenizer(scenario_type[i], max_length=self.model_args.max_token_len, padding='max_length')["input_ids"]).unsqueeze(0))
            scenario_tag_ids = torch.stack(scenario_tag_ids, dim=0).to(device)
            scenario_tag_embeds = self.tag_embedding(scenario_tag_ids).squeeze(1)
            assert scenario_tag_embeds.shape[1] == self.model_args.max_token_len, f'{scenario_tag_embeds.shape} vs {self.model_args.max_token_len}'
            input_embeds = torch.cat([scenario_tag_embeds, input_embeds], dim=1)

        if self.ar_future_interval == 0:
            # to keep input and output at the same dimension
            input_embeds = torch.cat([input_embeds,
                                      torch.zeros((batch_size, pred_length, n_embed), device=device)], dim=1)
            # attention_mask = torch.ones((input_embeds.shape[0], input_embeds.shape[1]), device=device)
            # attention_mask[:, context_length * 2:] = 0
        elif self.ar_future_interval > 0:
            # use autoregressive future interval
            if self.model_args.specified_key_points:
                # 80, 40, 20, 10, 5
                if self.model_args.forward_specified_key_points:
                    selected_indices = [4, 9, 19, 39, 79]
                else:
                    selected_indices = [79, 39, 19, 9, 4]
                future_key_points = trajectory_label[:, selected_indices, :]
                future_key_points_gt_mask = trajectory_label_mask[:, selected_indices, :]
            else:
                future_key_points = trajectory_label[:, self.ar_future_interval - 1::self.ar_future_interval, :]
                future_key_points_gt_mask = trajectory_label_mask[:, self.ar_future_interval - 1::self.ar_future_interval, :]
                
            assert future_key_points.shape[1] != 0, 'future points not enough to sample'

            future_key_points_aug = future_key_points.clone()
            if self.model_args.arf_x_random_walk > 0 and self.training:
                x_noise = torch.rand(future_key_points.shape, device=device) * self.model_args.arf_x_random_walk * 2 - self.model_args.arf_x_random_walk
                # add progressive scale, the future points the larger noise
                if self.model_args.specified_key_points:
                    indices = torch.tensor(selected_indices, device=device, dtype=float) / 80.0
                else:
                    indices = torch.arange(future_key_points.shape[1], device=device) / future_key_points.shape[1]
                expanded_indices = indices.unsqueeze(0).unsqueeze(-1).expand(future_key_points.shape)
                x_noise = x_noise * expanded_indices
                future_key_points_aug[:, :, 0] += x_noise[:, :, 0]
            if self.model_args.arf_y_random_walk > 0 and self.training:
                y_noise = torch.rand(future_key_points.shape, device=device) * self.model_args.arf_y_random_walk * 2 - self.model_args.arf_y_random_walk
                expanded_indices = indices.unsqueeze(0).unsqueeze(-1).expand(future_key_points.shape)
                y_noise = y_noise * expanded_indices
                future_key_points_aug[:, :, 1] += y_noise[:, :, 1]

            if not self.model_args.predict_yaw:
                # keep the same information when generating future points
                future_key_points_aug[:, :, 2:] = 0

            future_key_embeds = self.action_m_embed(future_key_points_aug)
            input_embeds = torch.cat([input_embeds, future_key_embeds,
                                      torch.zeros((batch_size, pred_length, n_embed), device=device)], dim=1)
            # attention_mask = torch.ones((input_embeds.shape[0], input_embeds.shape[1]), device=device)
            # attention_mask[:, context_length * 2 + future_key_embeds.shape[1]:] = 0
        else:
            raise ValueError("ar_future_interval should be non-negative", self.ar_future_interval)

        transformer_outputs = self.transformer(
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=input_embeds,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        transformer_outputs_hidden_state = transformer_outputs['last_hidden_state']

        traj_hidden_state = transformer_outputs_hidden_state[:, -pred_length - 1:-1, :]
        # expected shape for pred trajectory is (b, pred_length, 4)
        loss = torch.tensor(0, dtype=torch.float32, device=device)
        if 'mse' in self.loss_fn:
            loss_fct = nn.MSELoss(reduction="mean")
        elif 'l1' in self.loss_fn:
            loss_fct = nn.SmoothL1Loss()
        if not self.model_args.pred_key_points_only:
            traj_logits = self.traj_decoder(traj_hidden_state)
            if self.task == "waymo":
                loss_fct = MSELoss(reduction="none")
                # y_mask = ((trajectory_label != -1).sum(-1) > 0).view(batch_size, pred_length, 1)
                _loss = (loss_fct(traj_logits[..., :2], trajectory_label[..., :2].to(device)) * trajectory_label_mask).sum() / (
                            trajectory_label_mask.sum() + 1e-7)
                loss += _loss
            else:
                if self.model_args.predict_yaw:
                    loss += loss_fct(traj_logits, trajectory_label.to(device)) * self.model_args.trajectory_loss_rescale
                else:
                    loss += loss_fct(traj_logits[..., :2], trajectory_label[...,
                                                           :2].to(device)) * self.model_args.trajectory_loss_rescale
        else:
            traj_logits = torch.zeros_like(trajectory_label[..., :2])

        if self.ar_future_interval > 0:
            """
            for example:
            context_length = 2
            FutureKeyPoints = 2
            input_embed: [O, A, O, A, FutureKey1, FutureKey2, Traj1(Given0), Traj2(Given0)..]
            output_embed: [A, O, A, FutureKey1, FutureKey2, Traj1, Traj2.., x(Attentionally Blank)]
            """
            scenario_type_len = self.model_args.max_token_len if self.model_args.token_scenario_tag else 0
            future_key_points_hidden_state = transformer_outputs_hidden_state[:, scenario_type_len + context_length * 2 - 1:scenario_type_len + context_length * 2 + future_key_points.shape[1] - 1, :]
            key_points_logits = self.key_points_decoder(future_key_points_hidden_state)  # b, s, 4/2*k

            if self.k == 1:
                if self.model_args.predict_yaw:
                    loss_to_add = loss_fct(key_points_logits, future_key_points.to(device))
                else:
                    loss_to_add = loss_fct(key_points_logits, future_key_points[..., :2].to(device))
                
                if self.task == "waymo":
                    loss_to_add = (loss_to_add* future_key_points_gt_mask).sum() / (future_key_points_gt_mask.sum() + 1e-7)
                loss += loss_to_add
                traj_logits = torch.cat([key_points_logits, traj_logits], dim=1)
            else:
                b, s, c = future_key_points.shape
                k_results = key_points_logits.reshape(b, s, self.k, -1)

                # get loss of minimal loss from k results
                k_future_key_points = future_key_points.unsqueeze(2).repeat(1, 1, self.k, 1).reshape(b, s, self.k, -1)
                loss_fct_key_points = MSELoss(reduction="none")
                if self.model_args.predict_yaw:
                    loss_to_add = loss_fct_key_points(k_results, k_future_key_points.to(device))
                else:
                    loss_to_add = loss_fct_key_points(k_results, k_future_key_points[..., :2].to(device))
                # add loss on x, y (the last dimension)
                loss_to_add = loss_to_add.sum(dim=-1)  # b, s, k
                min_loss, min_loss_indices = torch.min(loss_to_add, dim=2)  # b, s
                
                if self.task == "waymo":
                    loss += (min_loss.unsqueeze(-1) * future_key_points_gt_mask).sum() / (future_key_points_gt_mask.sum() + 1e-7)
                else:
                    loss += min_loss.mean()
                if self.next_token_scorer_decoder is not None:
                    pred_logits = self.next_token_scorer_decoder(future_key_points_hidden_state.to(device))  # b, s, k
                    loss_fct = CrossEntropyLoss(reduction="mean")
                    loss_to_add = loss_fct(pred_logits.reshape(b * s, self.k).to(torch.float64), min_loss_indices.reshape(-1).long())
                    loss += loss_to_add
                    if self.training:
                        # concatenate the key points with predicted trajectory for evaluation
                        selected_key_points = key_points_logits.reshape(b * s, self.k, -1)[torch.arange(b * s),
                                              min_loss_indices.reshape(-1), :].reshape(b, s, -1)
                    else:
                        # concatenate the key points with predicted trajectory selected from the classifier for evaluation
                        selected_key_points = key_points_logits.reshape(b * s, self.k, -1)[torch.arange(b * s),
                                              pred_logits.argmax(dim=-1).reshape(-1), :].reshape(b, s, -1)
                    traj_logits = torch.cat([selected_key_points, traj_logits], dim=1)
                else:
                    print('WARNING: Randomly select key points for evaluation, try to use next_token_scorer_decoder')
                    traj_logits = torch.cat([key_points_logits[0].reshape(b, s, -1), traj_logits], dim=1)

        # evaluate accuracy if on eval
        if not self.training and self.clf_metrics is not None:
            if self.next_token_scorer_decoder is not None:
                # classification on k predictions
                predictions = torch.argmax(pred_logits, dim=-1)  # b, s, k
                for _, metric in self.clf_metrics.items():
                    metric.add_batch(references=min_loss_indices.reshape(-1), predictions=predictions.reshape(-1))

        if not return_dict:
            output = (traj_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithCrossAttentions(
            loss=loss,
            logits=traj_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
            cross_attentions=transformer_outputs.cross_attentions,
        )

    @torch.no_grad()
    def generate(self, batch_size, input_dict, batch_sample_count, **kwargs) -> torch.FloatTensor:
        
        batch_size = input_dict['obj_trajs'].shape[0]
        # encoder
        device = input_dict['obj_trajs'].device
        batch_dict = self.context_encoder({'input_dict': input_dict, 
                                           'batch_sample_count': batch_sample_count})
        
        obj_feature = batch_dict['obj_feature']
        map_feature = batch_dict['map_feature']
        state_embeds = torch.cat((map_feature, obj_feature), dim=1) # (bs, num_poly+num_obj, num_timestamp, 256)
        state_embeds = state_embeds.max(dim=1)[0]
        
        # traj
        trajectory_label = input_dict['trajectory_label']
        pred_length = trajectory_label.shape[1]
        trajectory_label_mask = input_dict['center_gt_trajs_mask'].unsqueeze(-1)
        
        # action context
        context_actions = input_dict['center_objects_past']
        if self.model_args.x_random_walk > 0 and self.training:
            x_noise = torch.rand(context_actions.shape, device=device) * self.model_args.x_random_walk * 2 - self.model_args.x_random_walk
            context_actions[:, :, 0] += x_noise[:, :, 0]
        if self.model_args.y_random_walk > 0 and self.training:
            y_noise = torch.rand(context_actions.shape, device=device) * self.model_args.y_random_walk * 2 - self.model_args.y_random_walk
            context_actions[:, :, 1] += y_noise[:, :, 1]

        action_embeds = self.action_m_embed(context_actions)
        context_length = context_actions.shape[1]  # past_interval=10, past_frames=2 * 20, context_length = 40/10=4

        # create OAOAOA..
        n_embed = action_embeds.shape[-1]
        input_embeds = torch.zeros(
            (batch_size, context_length * 2, n_embed),
            dtype=torch.float32,
            device=device
        )
        input_embeds[:, ::2, :] = state_embeds  # index: 0, 2, 4, .., 18
        input_embeds[:, 1::2, :] = action_embeds  # index: 1, 3, 5, .., 19
        
        if self.model_args.token_scenario_tag:
            scenario_type = kwargs.get("scenario_type", None)
            scenario_tag_ids = list()
            for i in range(batch_size):
                scenario_tag_ids.append(torch.tensor(self.tag_tokenizer(scenario_type[i], max_length=self.model_args.max_token_len, padding='max_length')["input_ids"]).unsqueeze(0))
            scenario_tag_ids = torch.stack(scenario_tag_ids, dim=0).to(device)
            scenario_tag_embeds = self.tag_embedding(scenario_tag_ids).squeeze(1)
            input_embeds = torch.cat([scenario_tag_embeds, input_embeds], dim=1)
            scenario_type_len = self.model_args.max_token_len
        else:
            scenario_type_len = 0

        assert self.ar_future_interval > 0, 'ar_future_interval should be larger than 0, else do not use generate'
        # use autoregressive future interval
        trajectory_label_dummy = torch.zeros((batch_size, pred_length, 4), device=device)
        if self.model_args.specified_key_points:
            # 80, 40, 20, 10, 5
            if self.model_args.forward_specified_key_points:
                selected_indices = [4, 9, 19, 39, 79]
            else:
                selected_indices = [79, 39, 19, 9, 4]
            future_key_points = trajectory_label_dummy[:, selected_indices, :]
        else:
            future_key_points = trajectory_label_dummy[:, self.ar_future_interval - 1::self.ar_future_interval, :]
        assert future_key_points.shape[1] > 0, 'future points not enough to sample'
        future_key_embeds_dummy = self.action_m_embed(future_key_points)
        input_embeds = torch.cat([input_embeds, future_key_embeds_dummy,
                                  torch.zeros((batch_size, pred_length, n_embed), device=device)], dim=1)
        key_points_num = future_key_points.shape[1]
        pred_key_points_during_generate = []
        # attention_mask[:, context_length * 2 + future_key_embeds.shape[1]:] = 0
        # Loop for generation
        for i in range(key_points_num):
            # prepare attention mask
            # attention_mask = torch.ones((input_embeds.shape[0], input_embeds.shape[1]), device=device)
            # attention_mask[:, context_length * 2 + i + 1:] = 0
            # position_ids = self._prepare_position_ids_for_generation(attention_mask.clone())
            input_embeds_current = input_embeds[:, :scenario_type_len + context_length * 2 + i, :]
            attention_mask = torch.ones(input_embeds_current.shape[:2], dtype=torch.long, device=input_embeds.device)
            position_ids = self._prepare_position_ids_for_generation(attention_mask.clone())
            transformer_output = self.transformer(
                inputs_embeds=input_embeds_current,
                attention_mask=attention_mask,
                position_ids=position_ids,
                # **input_kwargs
            )
            transformer_outputs_hidden_state = transformer_output['last_hidden_state']
            future_key_point_hidden_state = transformer_outputs_hidden_state[:, scenario_type_len + context_length * 2 + i - 1, :].reshape(batch_size, 1, -1)

            if self.k > 1:
                key_points_logit = self.key_points_decoder(future_key_point_hidden_state).reshape(batch_size, 1, -1)  # b, 1, 4/2*k
                pred_logits = self.next_token_scorer_decoder(future_key_point_hidden_state.to(device)).reshape(batch_size, 1, -1)  # b, 1, k
                selected_key_point = key_points_logit.reshape(batch_size, self.k, -1)[torch.arange(batch_size), pred_logits.argmax(dim=-1).reshape(-1), :].reshape(batch_size, 1, -1)
                key_points_logit = selected_key_point
            else:
                key_points_logit = self.key_points_decoder(future_key_point_hidden_state).reshape(batch_size, 1, -1)  # b, 1, 4/2
            pred_key_point = torch.zeros((batch_size, 1, 4), device=device)
            if self.model_args.predict_yaw:
                pred_key_point[:, 0, :] = key_points_logit[:, 0, :]
            else:
                pred_key_point[:, 0, :2] = key_points_logit[:, 0, :]

            key_point_embed = self.action_m_embed(pred_key_point).reshape(batch_size, 1, -1)  # b, 1, n_embed
            # replace embed at the next position
            input_embeds[:, scenario_type_len + context_length * 2 + i, :] = key_point_embed[:, 0, :]
            pred_key_points_during_generate.append(pred_key_point[:, 0, :2].unsqueeze(1))
        # generate remaining trajectory
        # prepare attention mask
        # attention_mask = torch.ones((input_embeds.shape[0], input_embeds.shape[1]), device=device)
        # attention_mask[:, context_length * 2 + key_points_num:] = 0
        # position_ids = self._prepare_position_ids_for_generation(attention_mask.clone())
        transformer_output = self.transformer(
            inputs_embeds=input_embeds,
            # attention_mask=attention_mask,
            attention_mask=None,
            position_ids=None,
            # **input_kwargs
        )
        transformer_outputs_hidden_state = transformer_output['last_hidden_state']
        traj_hidden_state = transformer_outputs_hidden_state[:, -pred_length-1:-1, :]
        # expected shape for pred trajectory is (b, pred_length, 4)
        if self.traj_decoder is not None:
            traj_logits = self.traj_decoder(traj_hidden_state)
        else:
            traj_logits = trajectory_label_dummy[..., :2]
        future_key_points_hidden_state = transformer_outputs_hidden_state[:, scenario_type_len + context_length * 2 - 1:scenario_type_len + context_length * 2 + future_key_points.shape[1] - 1, :]

        if self.k > 1:
            key_points_logits = self.key_points_decoder(future_key_points_hidden_state)  # b, s, 4/2*k
            pred_logits = self.next_token_scorer_decoder(future_key_points_hidden_state.to(device))  # b, s, k
            selected_key_points = key_points_logits.reshape(batch_size * key_points_num, self.k, -1)[torch.arange(batch_size * key_points_num),
                                  pred_logits.argmax(dim=-1).reshape(-1), :].reshape(batch_size, key_points_num, -1)
            key_points_logits = selected_key_points
        elif self.k == 1:
            key_points_logits = self.key_points_decoder(future_key_points_hidden_state)  # b, s, 4/2
            # use previous prediction during generation
            key_points_logits = torch.cat(pred_key_points_during_generate, dim=1).reshape(key_points_logits.shape)
        else:
            raise ValueError("illegal k while generating trajectory", self.k)

        # return torch.cat([key_points_logits, traj_logits], dim=1)
        return {'key_points_logits': key_points_logits, 'logits': traj_logits}