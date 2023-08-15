from transformers import GPT2Model, GPT2PreTrainedModel
from transformer4planning.models.GPT2.models import *
from transformer4planning.models.decoders import *
from transformer4planning.models.utils import *
from transformer4planning.utils import *
from transformer4planning.models.decoder.diffusion_decoder import DiffusionDecoderTFBasedForKeyPoints
import torch.nn as nn

class TrajectoryGPTFeatureGen(GPT2PreTrainedModel):
    r"""
        This is exactly the same class as transformer4planning.models.TrajectoryGPT except that this class is adjusted for saving features for training the KeyPointDiffusionDecoder.
    """
    
    
    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.transformer = GPT2Model(config)
        self.model_args = kwargs["model_args"]
        assert not self.model_args.interactive, 'Not supported.'
        self.traj_decoder = None
        self.k = int(self.model_args.k)
        self.ar_future_interval = self.model_args.ar_future_interval
        self.model_parallel = False
        self.device_map = None

        self.next_token_scorer_decoder = None
        self.key_points_decoder = None
        out_features = 4 if self.model_args.predict_yaw else 2
        if not self.model_args.pred_key_points_only:
            self.traj_decoder = DecoderResCat(config.n_inner, config.n_embd, out_features=out_features)
        if self.ar_future_interval > 0:
            self.key_points_decoder = DecoderResCat(config.n_inner, config.n_embd, out_features=out_features * self.k)
        if self.k > 1:
            self.next_token_scorer_decoder = DecoderResCat(config.n_inner, config.n_embd, out_features=self.k)

        self.clf_metrics = None
        # Initialize weights and apply final processing
        self.post_init()
        self.build_encoder()
        
        assert self.model_args.generate_diffusion_dataset_for_key_points_decoder, 'This model should only be used for generating diffusion dataset for Diffusion Key Point Decoder.'

        self.task = self.model_args.task
        self.encoder_type = self.model_args.encoder_type
        if self.model_args.generate_diffusion_dataset_for_key_points_decoder:
            assert self.ar_future_interval > 0, ''
            self.save_training_diffusion_dataset_dir = os.path.join(self.model_args.diffusion_dataset_save_dir,'train/')
            self.save_testing_diffusion_dataset_dir  = os.path.join(self.model_args.diffusion_dataset_save_dir,'test/')
            if not os.path.exists(self.save_training_diffusion_dataset_dir):
                os.makedirs(self.save_training_diffusion_dataset_dir)
            if not os.path.exists(self.save_testing_diffusion_dataset_dir):
                os.makedirs(self.save_testing_diffusion_dataset_dir)
            self.current_idx = 0
            self.gpu_device_count = torch.cuda.device_count()
            # Notice that although we check and create two directories (train/ and test/) here, in the forward method we only save features in eval loops.
            # This is because evaluation is way faster than training (since there are no backward propagation), and after saving features for evaluation, we just change our test set to training set and then run the evaluation loop again.
            # The related code can be found in runner.py at around line 511.
    
    def build_encoder(self):
        if self.model_args.task == "nuplan":
            # TODO: add raster/vector encoder configuration item
            tokenizer_kwargs = dict(
                dirpath=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gpt2-tokenizer'),
                d_embed=self.config.n_embd,
            )
            
            if "raster" in self.model_args.encoder_type:
                from transformer4planning.models.encoder import NuplanRasterizeEncoder
                cnn_kwargs = dict(
                    d_embed=self.config.n_embd // 2,
                    in_channels=self.model_args.raster_channels,
                    resnet_type=self.model_args.resnet_type, 
                    pretrain=self.model_args.pretrain_encoder
                )
                action_kwargs = dict(
                    d_embed=self.config.n_embd
                )
                self.encoder = NuplanRasterizeEncoder(cnn_kwargs, action_kwargs, tokenizer_kwargs, self.model_args)
            elif "vector" in self.model_args.encoder_type:
                from transformer4planning.models.encoder import PDMEncoder
                pdm_kwargs = dict(
                    hidden_dim=self.config.n_embd,
                    centerline_dim=120,
                    history_dim=20
                )
                self.encoder = PDMEncoder(pdm_kwargs, tokenizer_kwargs, self.model_args)
            else:
                raise AttributeError("encoder_type should be either raster or vector")
        elif self.model_args.task == "waymo":
            from transformer4planning.models.encoder.mtr_encoder import WaymoVectorizeEncoder
            from dataset_gen.waymo.config import cfg_from_yaml_file, cfg
            cfg_from_yaml_file(self.model_args.mtr_config_path, cfg)
            action_kwargs = dict(
                    d_embed=self.config.n_embd
                )
            tokenizer_kwargs = dict(
                dirpath=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gpt2-tokenizer'),
                d_embed=self.config.n_embd,
                max_token_len=self.model_args.max_token_len,
            ) if self.model_args.token_scenario_tag else None
            self.encoder = WaymoVectorizeEncoder(cfg, action_kwargs, tokenizer_kwargs, self.model_args)
        else:
            raise NotImplementedError
    def _prepare_attention_mask_for_generation(self, input_embeds):
        return torch.ones(input_embeds.shape[:2], dtype=torch.long, device=input_embeds.device)

    def _prepare_position_ids_for_generation(self, attention_mask):
        position_ids = attention_mask.long().cumsum(-1) - 1
        position_ids.masked_fill_(attention_mask == 0, 1)
        return position_ids
    
    def forward(
            self,
            trajectory_label: Optional[torch.FloatTensor] = None,
            context_actions: Optional[torch.FloatTensor] = None,
            high_res_raster: Optional[torch.LongTensor] = None,
            low_res_raster: Optional[torch.LongTensor] = None,
            scenario_type: Optional[str] = None,
            return_dict: Optional[bool] = None,
            **kwargs
    ):
        # gpt non-autoregression version
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        device = high_res_raster.device
        if self.model_args.generate_diffusion_dataset_for_key_points_decoder:
            current_device_idx = int(str(high_res_raster.device)[-1])
        batch_size, pred_length = trajectory_label.shape[:2]
        context_length = context_actions.shape[1]  # past_interval=10, past_frames=2 * 20, context_length = 40/10=4
        feature_inputs = dict(
            high_res_raster=high_res_raster,
            low_res_raster=low_res_raster,
            context_actions=context_actions,
            trajectory_label=trajectory_label,
            scenario_type=scenario_type,
            pred_length=pred_length,
            context_length=context_length,
        )
        input_embeds, info_dict = self.encoder(**feature_inputs)
        attention_mask = info_dict["input_embeds_mask"] if self.model_args.interactive else None
        transformer_outputs = self.transformer(
            inputs_embeds=input_embeds,
            attention_mask=attention_mask,
            return_dict=return_dict,
            # **kwargs
        )

        transformer_outputs_hidden_state = transformer_outputs['last_hidden_state']

        traj_hidden_state = transformer_outputs_hidden_state[:, -pred_length - 1:-1, :]
        # expected shape for pred trajectory is (b, pred_length, 4)
        loss = torch.tensor(0, dtype=torch.float32, device=device)
        if 'mse' in self.model_args.loss_fn:
            loss_fct = nn.MSELoss(reduction="mean")
        elif 'l1' in self.model_args.loss_fn:
            loss_fct = nn.SmoothL1Loss()
        if not self.model_args.pred_key_points_only:
            traj_logits = self.traj_decoder(traj_hidden_state)
            if self.model_args.task == "waymo":
                loss_fct = MSELoss(reduction="none")
                y_mask = ((trajectory_label != -1).sum(-1) > 0).view(batch_size, pred_length, 1)
                _loss = (loss_fct(traj_logits[..., :2], trajectory_label[..., :2].to(device)) * y_mask).sum() / (y_mask.sum() + 1e-7)
                loss += _loss
            else:
                if self.model_args.predict_yaw:
                    loss += loss_fct(traj_logits, trajectory_label.to(device)) * self.model_args.trajectory_loss_rescale
                else:
                    loss += loss_fct(traj_logits[..., :2], trajectory_label[..., :2].to(device)) * self.model_args.trajectory_loss_rescale
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
            future_key_points = info_dict["future_key_points"]
            scenario_type_len = self.model_args.max_token_len if self.model_args.token_scenario_tag else 0
            # future_key_points_hidden_state = transformer_outputs_hidden_state[:, scenario_type_len + context_length * 2 - 1:scenario_type_len + context_length * 2 + future_key_points.shape[1] - 1, :]
            # For auto-regressive, future key point hidden state is the previous one.
            # But for diffusion, it is 0:scenario_type_len+context_length*2 that is the condition for future key points:
            future_key_points_hidden_state = transformer_outputs_hidden_state[:, :scenario_type_len + context_length * 2, :]
            if self.model_args.generate_diffusion_dataset_for_key_points_decoder:
                # current_device_idx = int(str(traj_hidden_state.device)[-1])
                save_id = self.gpu_device_count * self.current_idx + current_device_idx
                if self.training:
                    torch.save(future_key_points_hidden_state.detach().cpu(),os.path.join(self.save_training_diffusion_dataset_dir, f'future_key_points_hidden_state_{save_id}.pth'), )
                    torch.save(future_key_points.detach().cpu(), os.path.join(self.save_training_diffusion_dataset_dir, f'future_key_points_{save_id}.pth'), )
                else:
                    torch.save(future_key_points_hidden_state.detach().cpu(),os.path.join(self.save_testing_diffusion_dataset_dir, f'future_key_points_hidden_state_{save_id}.pth'), )
                    torch.save(future_key_points.detach().cpu(), os.path.join(self.save_testing_diffusion_dataset_dir, f'future_key_points_{save_id}.pth'), )
                self.current_idx += 1 # This would be executed on every gpu.
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
                
            assert False, ''
            return None
        #     key_points_logits = self.key_points_decoder(future_key_points_hidden_state)  # b, s, 4/2*k
        #     if self.k == 1:
        #         if self.model_args.predict_yaw:
        #             loss_to_add = loss_fct(key_points_logits, future_key_points.to(device))
        #         else:
        #             loss_to_add = loss_fct(key_points_logits, future_key_points[..., :2].to(device))
        #         loss += loss_to_add
        #         traj_logits = torch.cat([key_points_logits, traj_logits], dim=1)
        #     else:
        #         b, s, c = future_key_points.shape
        #         k_results = key_points_logits.reshape(b, s, self.k, -1)

        #         # get loss of minimal loss from k results
        #         k_future_key_points = future_key_points.unsqueeze(2).repeat(1, 1, self.k, 1).reshape(b, s, self.k, -1)
        #         loss_fct_key_points = MSELoss(reduction="none")
        #         if self.model_args.predict_yaw:
        #             loss_to_add = loss_fct_key_points(k_results, k_future_key_points.to(device))
        #         else:
        #             loss_to_add = loss_fct_key_points(k_results, k_future_key_points[..., :2].to(device))
        #         # add loss on x, y (the last dimension)
        #         loss_to_add = loss_to_add.sum(dim=-1)  # b, s, k
        #         min_loss, min_loss_indices = torch.min(loss_to_add, dim=2)  # b, s
        #         loss += min_loss.mean()
        #         if self.next_token_scorer_decoder is not None:
        #             pred_logits = self.next_token_scorer_decoder(future_key_points_hidden_state.to(device))  # b, s, k
        #             loss_fct = CrossEntropyLoss(reduction="mean")
        #             loss_to_add = loss_fct(pred_logits.reshape(b * s, self.k).to(torch.float64), min_loss_indices.reshape(-1).long())
        #             loss += loss_to_add
        #             if self.training:
        #                 # concatenate the key points with predicted trajectory for evaluation
        #                 selected_key_points = key_points_logits.reshape(b * s, self.k, -1)[torch.arange(b * s),
        #                                       min_loss_indices.reshape(-1), :].reshape(b, s, -1)
        #             else:
        #                 # concatenate the key points with predicted trajectory selected from the classifier for evaluation
        #                 selected_key_points = key_points_logits.reshape(b * s, self.k, -1)[torch.arange(b * s),
        #                                       pred_logits.argmax(dim=-1).reshape(-1), :].reshape(b, s, -1)
        #             traj_logits = torch.cat([selected_key_points, traj_logits], dim=1)
        #         else:
        #             print('WARNING: Randomly select key points for evaluation, try to use next_token_scorer_decoder')
        #             traj_logits = torch.cat([key_points_logits[0].reshape(b, s, -1), traj_logits], dim=1)

        # # evaluate accuracy if on eval
        # if not self.training and self.clf_metrics is not None:
        #     if self.next_token_scorer_decoder is not None:
        #         # classification on k predictions
        #         predictions = torch.argmax(pred_logits, dim=-1)  # b, s, k
        #         for _, metric in self.clf_metrics.items():
        #             metric.add_batch(references=min_loss_indices.reshape(-1), predictions=predictions.reshape(-1))

        # if not return_dict:
        #     output = (traj_logits,) + transformer_outputs[1:]
        #     return ((loss,) + output) if loss is not None else output

        # return CausalLMOutputWithCrossAttentions(
        #     loss=loss,
        #     logits=traj_logits,
        #     past_key_values=transformer_outputs.past_key_values,
        #     hidden_states=transformer_outputs.hidden_states,
        #     attentions=transformer_outputs.attentions,
        #     cross_attentions=transformer_outputs.cross_attentions,
        # )

    @torch.no_grad()
    def generate(self, **kwargs) -> torch.FloatTensor:
        high_res_raster = kwargs.get("high_res_raster", None)
        low_res_raster = kwargs.get("low_res_raster", None)
        pred_length = kwargs.get("pred_length", None)
        trajectory_label = kwargs.get("trajectory_label", None)
        context_actions = kwargs.get("context_actions", None)
        # pass the following infos during generate for one sample (non-batch) generate with KP checking
        map_api = kwargs.get("map_api", None)
        route_ids = kwargs.get("route_ids", None)
        ego_pose = kwargs.get("ego_pose", None)
        road_dic = kwargs.get("road_dic", None)
        scenario_type = kwargs.get("scenario_type", None)
        idm_reference_global = kwargs.get("idm_reference_global", None)
        """
        Used for generate with key points
        """
        device = high_res_raster.device
        batch_size, pred_length = trajectory_label.shape[:2]
        context_length = context_actions.shape[1]
        feature_inputs = dict(
            high_res_raster=high_res_raster,
            low_res_raster=low_res_raster,
            context_actions=context_actions,
            trajectory_label=trajectory_label,
            scenario_type=scenario_type,
            pred_length=pred_length,
            context_length=context_length,
        )
        input_embeds, info_dict = self.encoder(**feature_inputs)
        selected_indices = info_dict["selected_indices"]
        scenario_type_len = self.model_args.max_token_len if self.model_args.token_scenario_tag else 0

        assert self.ar_future_interval > 0, 'ar_future_interval should be larger than 0, else do not use generate'
        trajectory_label_dummy = torch.zeros((batch_size, pred_length, 4), device=device)
        if self.model_args.specified_key_points:
            future_key_points = trajectory_label_dummy[:, selected_indices, :]
        else:
            future_key_points = trajectory_label_dummy[:, self.ar_future_interval - 1::self.ar_future_interval, :]
        assert future_key_points.shape[1] > 0, 'future points not enough to sample'
        future_key_embeds_dummy = self.encoder.action_m_embed(future_key_points)
        key_points_num = future_key_points.shape[1]
        input_embeds[:, scenario_type_len + context_length * 2:scenario_type_len + context_length * 2 + key_points_num, :] = future_key_embeds_dummy
        pred_key_points_during_generate = []
        # Loop for generation
        for i in range(key_points_num):
            input_embeds_current = input_embeds[:, :scenario_type_len + context_length * 2 + i, :]
            attention_mask = torch.ones(input_embeds_current.shape[:2], dtype=torch.long, device=input_embeds.device)
            position_ids = self._prepare_position_ids_for_generation(attention_mask.clone())
            transformer_output = self.transformer(
                inputs_embeds=input_embeds_current,
                attention_mask=attention_mask,
                position_ids=position_ids,
            )
            transformer_outputs_hidden_state = transformer_output['last_hidden_state']
            future_key_point_hidden_state = transformer_outputs_hidden_state[:,
                                            scenario_type_len + context_length * 2 + i - 1,
                                            :].reshape(batch_size, 1, -1)

            if self.k > 1:
                key_points_logit = self.key_points_decoder(future_key_point_hidden_state).reshape(batch_size, 1, -1)  # b, 1, 4/2*k
                pred_logits = self.next_token_scorer_decoder(future_key_point_hidden_state.to(device)).reshape(batch_size, 1, -1)  # b, 1, k
                selected_key_point = key_points_logit.reshape(batch_size, self.k, -1)[torch.arange(batch_size),
                                     pred_logits.argmax(dim=-1).reshape(-1), :].reshape(batch_size, 1, -1)
                key_points_logit = selected_key_point
            else:
                key_points_logit = self.key_points_decoder(future_key_point_hidden_state).reshape(batch_size, 1, -1)  # b, 1, 4/2
            pred_key_point = torch.zeros((batch_size, 1, 4), device=device)
            if self.model_args.predict_yaw:
                pred_key_point[:, 0, :] = key_points_logit[:, 0, :]
            else:
                pred_key_point[:, 0, :2] = key_points_logit[:, 0, :]

            off_road_checking = False
            if off_road_checking and batch_size == 1 and map_api is not None and route_ids is not None and road_dic is not None:
                # Check key points with map_api
                # WARNING: WIP, do not use
                pred_key_point_global = change_coordination(pred_key_point[0, 0, :2].cpu().numpy(),
                                                            ego_pose,
                                                            ego_to_global=True)
                closest_lane_road_dic = query_current_lane(map_api=map_api, target_point=pred_key_point_global)
                nearest = closest_lane_road_dic['road_id']
                nearest_lane = closest_lane_road_dic['lane_id']
                dist = closest_lane_road_dic['distance_to_road_block']
                if nearest not in route_ids or dist > 0.5:
                    # off-road, move to nearest lane according to PDMPath
                    dist = euclidean_distance(pred_key_point[0, 0, :2].cpu().numpy(), [0, 0])
                    interpolate_point = center_path.interpolate(np.array([dist]))[0]
                    print('test offroad correction: ', pred_key_point[0, 0, :2].cpu().numpy(), interpolate_point)
                    pred_key_point[0, 0, :2] = torch.tensor(interpolate_point, device=pred_key_point.device)

            if idm_reference_global is not None and i == key_points_num - 1 and not self.model_args.forward_specified_key_points:
                # replace last key point with IDM reference
                ego_state_global = idm_reference_global[selected_indices[-1]]
                idm_reference_lastpt_relative = change_coordination(np.array([ego_state_global.rear_axle.x,
                                                                              ego_state_global.rear_axle.y]),
                                                                    ego_pose,
                                                                    ego_to_global=False)
                print('replace last key point with IDM reference, index: ', selected_indices[-1], pred_key_point[0, 0, :2], idm_reference_lastpt_relative)  # idm relative has an unusual large negative y value?
                pred_key_point[0, 0, :2] = torch.tensor(idm_reference_lastpt_relative, device=pred_key_point.device)
            key_point_embed = self.encoder.action_m_embed(pred_key_point).reshape(batch_size, 1, -1)  # b, 1, n_embed
            # replace embed at the next position
            input_embeds[:, scenario_type_len + context_length * 2 + i, :] = key_point_embed[:, 0, :]
            if self.model_args.predict_yaw:
                pred_key_points_during_generate.append(pred_key_point[:, 0, :].unsqueeze(1))
            else:
                pred_key_points_during_generate.append(pred_key_point[:, 0, :2].unsqueeze(1))

        # generate remaining trajectory
        transformer_output = self.transformer(
            inputs_embeds=input_embeds,
            attention_mask=None,
            position_ids=None,
        )
        transformer_outputs_hidden_state = transformer_output['last_hidden_state']
        traj_hidden_state = transformer_outputs_hidden_state[:, -pred_length - 1:-1, :]
        # expected shape for pred trajectory is (b, pred_length, 4)
        if self.traj_decoder is not None:
            traj_logits = self.traj_decoder(traj_hidden_state)
        else:
            traj_logits = trajectory_label_dummy[..., :2]
        future_key_points_hidden_state = transformer_outputs_hidden_state[:, scenario_type_len + context_length * 2 - 1:scenario_type_len + context_length * 2 + future_key_points.shape[1] - 1, :]

        if self.k > 1:
            key_points_logits = self.key_points_decoder(future_key_points_hidden_state)  # b, s, 4/2*k
            pred_logits = self.next_token_scorer_decoder(future_key_points_hidden_state.to(device))  # b, s, k
            selected_key_points = key_points_logits.reshape(batch_size * key_points_num, self.k, -1)[
                                  torch.arange(batch_size * key_points_num),
                                  pred_logits.argmax(dim=-1).reshape(-1),
                                  :].reshape(batch_size, key_points_num, -1)
            key_points_logits = selected_key_points
        elif self.k == 1:
            key_points_logits = self.key_points_decoder(future_key_points_hidden_state)  # b, s, 4/2
            # use previous prediction during generation
            # print('inspect kp: ', key_points_logits, pred_key_points_during_generate)
            key_points_logits = torch.cat(pred_key_points_during_generate, dim=1).reshape(key_points_logits.shape)
        else:
            raise ValueError("illegal k while generating trajectory", self.k)
        # print('Inspect shape in model generate: ', key_points_logits.shape, traj_logits.shape)
        return torch.cat([key_points_logits, traj_logits], dim=1)


def query_current_lane(map_api, target_point):
    """
    Query the current road_block id and lane id given a point on the map with map_api from NuPlan.
    Args:
        map_api: NuPlan's Map Api
        target_point: [x, y, ..] in global coordination
    Returns:
        {
            'road_id': int,
            'lane_id': int,
            'distance_to_road_block': float,
            'distance_to_lane': float
        }
    """
    from nuplan.common.actor_state.state_representation import Point2D
    from nuplan.common.maps.maps_datatypes import SemanticMapLayer
    from nuplan_garage.planning.simulation.planner.pdm_planner.utils.pdm_path import PDMPath
    point2d = Point2D(target_point[0], target_point[1])
    nearest_road_block_id, distance_to_road_block = map_api.get_distance_to_nearest_map_object(
        point=point2d,
        layer=SemanticMapLayer.ROADBLOCK
    )
    nearest_road_blockc_id, distance_to_road_block_c = map_api.get_distance_to_nearest_map_object(
        point=point2d,
        layer=SemanticMapLayer.ROADBLOCK_CONNECTOR
    )
    nearest_lane_id, distance_to_lane = map_api.get_distance_to_nearest_map_object(
        point=point2d,
        layer=SemanticMapLayer.LANE
    )
    nearest_lanec_id, distance_to_lanec = map_api.get_distance_to_nearest_map_object(
        point=point2d,
        layer=SemanticMapLayer.LANE_CONNECTOR
    )
    # check if on route
    if distance_to_road_block < distance_to_road_block_c:
        nearest_road_blockc_id = int(nearest_road_block_id)
        dist_to_road_block = distance_to_road_block
    else:
        nearest_road_blockc_id = int(nearest_road_blockc_id)
        dist_to_road_block = distance_to_road_block_c
    if distance_to_lane < distance_to_lanec:
        nearest_lane = int(nearest_lane_id)
        dist_to_nearest_lane = distance_to_lane
    else:
        nearest_lane = int(nearest_lanec_id)
        dist_to_nearest_lane = distance_to_lanec
    return {
        'road_id': nearest_road_blockc_id,
        'lane_id': nearest_lane,
        'distance_to_road_block': dist_to_road_block,
        'distance_to_lane': dist_to_nearest_lane
    }

class TrajectoryGPTDiffusionKPDecoder(GPT2PreTrainedModel):
    def __init__(self, config, **kwargs):
        
        super().__init__(config, **kwargs)
        self.transformer = GPT2Model(config)
        self.model_args = kwargs["model_args"]
        assert not self.model_args.interactive, 'Not supported.'
        self.traj_decoder = None
        self.k = int(self.model_args.k)
        assert self.k==1,'Currently not supported.'
        self.ar_future_interval = self.model_args.ar_future_interval
        self.model_parallel = False
        self.device_map = None

        self.next_token_scorer_decoder = None
        self.key_points_decoder = None
        out_features = 4 if self.model_args.predict_yaw else 2
        self.task = self.model_args.task
        self.encoder_type = self.model_args.encoder_type
        if not self.model_args.pred_key_points_only:
            self.traj_decoder = DecoderResCat(config.n_inner, config.n_embd, out_features=out_features)
        if self.ar_future_interval > 0:
            self.key_points_decoder = DiffusionDecoderTFBasedForKeyPoints(config.n_inner, config.n_embd, out_features=out_features * self.k, num_key_points = self.model_args.key_points_num, input_feature_seq_lenth = self.model_args.diffusion_condition_sequence_lenth,
                                                                          specified_key_points = self.model_args.specified_key_points, forward_specified_key_points = self.model_args.forward_specified_key_points)
        if self.k > 1:
            self.next_token_scorer_decoder = DecoderResCat(config.n_inner, config.n_embd, out_features=self.k)

        self.clf_metrics = None
        # Initialize weights and apply final processing
        self.post_init()
        self.build_encoder()
        
    def build_encoder(self):
        if self.model_args.task == "nuplan":
            # TODO: add raster/vector encoder configuration item
            tokenizer_kwargs = dict(
                dirpath=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gpt2-tokenizer'),
                d_embed=self.config.n_embd,
            )
            
            if "raster" in self.model_args.encoder_type:
                from transformer4planning.models.encoder.encoders import NuplanRasterizeEncoder
                cnn_kwargs = dict(
                    d_embed=self.config.n_embd // 2,
                    in_channels=self.model_args.raster_channels,
                    resnet_type=self.model_args.resnet_type, 
                    pretrain=self.model_args.pretrain_encoder
                )
                action_kwargs = dict(
                    d_embed=self.config.n_embd
                )
                self.encoder = NuplanRasterizeEncoder(cnn_kwargs, action_kwargs, tokenizer_kwargs, self.model_args)
            elif "vector" in self.model_args.encoder_type:
                from transformer4planning.models.encoder.encoders import PDMEncoder
                pdm_kwargs = dict(
                    hidden_dim=self.config.n_embd,
                    centerline_dim=120,
                    history_dim=20
                )
                self.encoder = PDMEncoder(pdm_kwargs, tokenizer_kwargs, self.model_args)
            else:
                raise AttributeError("encoder_type should be either raster or vector")
        elif self.model_args.task == "waymo":
            from transformer4planning.models.encoder.mtr_encoder import WaymoVectorizeEncoder
            from dataset_gen.waymo.config import cfg_from_yaml_file, cfg
            cfg_from_yaml_file(self.model_args.mtr_config_path, cfg)
            action_kwargs = dict(
                    d_embed=self.config.n_embd
                )
            tokenizer_kwargs = dict(
                dirpath=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gpt2-tokenizer'),
                d_embed=self.config.n_embd,
                max_token_len=self.model_args.max_token_len,
            ) if self.model_args.token_scenario_tag else None
            self.encoder = WaymoVectorizeEncoder(cfg, action_kwargs, tokenizer_kwargs, self.model_args)
        else:
            raise NotImplementedError

    def _prepare_attention_mask_for_generation(self, input_embeds):
        return torch.ones(input_embeds.shape[:2], dtype=torch.long, device=input_embeds.device)

    def _prepare_position_ids_for_generation(self, attention_mask):
        position_ids = attention_mask.long().cumsum(-1) - 1
        position_ids.masked_fill_(attention_mask == 0, 1)
        return position_ids
    
    def forward(
            self,
            trajectory_label: Optional[torch.FloatTensor] = None,
            context_actions: Optional[torch.FloatTensor] = None,
            high_res_raster: Optional[torch.LongTensor] = None,
            low_res_raster: Optional[torch.LongTensor] = None,
            scenario_type: Optional[str] = None,
            return_dict: Optional[bool] = None,
            **kwargs
    ):
        # gpt non-autoregression version
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        device = high_res_raster.device
        batch_size, pred_length = trajectory_label.shape[:2]
        context_length = context_actions.shape[1]  # past_interval=10, past_frames=2 * 20, context_length = 40/10=4
        feature_inputs = dict(
            high_res_raster=high_res_raster,
            low_res_raster=low_res_raster,
            context_actions=context_actions,
            trajectory_label=trajectory_label,
            scenario_type=scenario_type,
            pred_length=pred_length,
            context_length=context_length,
        )
        input_embeds, info_dict = self.encoder(**feature_inputs)
        future_key_points = info_dict["future_key_points"]
        attention_mask = info_dict["input_embeds_mask"] if self.model_args.interactive else None
        transformer_outputs = self.transformer(
            inputs_embeds=input_embeds,
            return_dict=return_dict,
            attention_mask = attention_mask,
            # **kwargs
        )

        transformer_outputs_hidden_state = transformer_outputs['last_hidden_state']

        traj_hidden_state = transformer_outputs_hidden_state[:, -pred_length - 1:-1, :]
        # expected shape for pred trajectory is (b, pred_length, 4)
        loss = torch.tensor(0, dtype=torch.float32, device=device)
        if 'mse' in self.model_args.loss_fn:
            loss_fct = nn.MSELoss(reduction="mean")
        elif 'l1' in self.model_args.loss_fn:
            loss_fct = nn.SmoothL1Loss()
        if not self.model_args.pred_key_points_only:
            traj_logits = self.traj_decoder(traj_hidden_state)
            if self.model_args.task == "waymo":
                trajectory_label_mask = info_dict["trajectory_label_mask"]
                loss_fct = MSELoss(reduction="none")
                _loss = (loss_fct(traj_logits[..., :2], trajectory_label[..., :2].to(device)) * trajectory_label_mask).sum() / (
                            trajectory_label_mask.sum() + 1e-7)
                loss += _loss
            else:
                if self.model_args.predict_yaw:
                    loss += loss_fct(traj_logits, trajectory_label.to(device)) * self.model_args.trajectory_loss_rescale
                else:
                    loss += loss_fct(traj_logits[..., :2], trajectory_label[..., :2].to(device)) * self.model_args.trajectory_loss_rescale
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
            future_key_points = info_dict["future_key_points"]
            scenario_type_len = self.model_args.max_token_len if self.model_args.token_scenario_tag else 0
            # This is the original code, which is changed since it does not suit the diffusion decoder for KP.
            # future_key_points_hidden_state = transformer_outputs_hidden_state[:, scenario_type_len + context_length * 2 - 1:scenario_type_len + context_length * 2 + future_key_points.shape[1] - 1, :]
            # key_points_logits = self.key_points_decoder(future_key_points_hidden_state)  # b, s, 4/2*k
            assert self.model_args.task == 'nuplan', 'TODO: not supported.' # TODO: support Waymo.
            if self.k == 1:
                if self.training:
                    feature_for_keypointdiffusion = transformer_outputs_hidden_state[:, :scenario_type_len + context_length * 2, :]
                    gt_future_key_points = future_key_points.to(device) if self.model_args.predict_yaw else future_key_points[..., :2].to(device)
                    # print('gt_future_key_poins.shape==',gt_future_key_points.shape)
                    loss_to_add_for_key_point_diffusion = self.key_points_decoder.train_forward(feature_for_keypointdiffusion, gt_future_key_points)
                    loss += loss_to_add_for_key_point_diffusion
                    traj_logits = torch.cat([gt_future_key_points, traj_logits], dim=1) * 0
                else:
                    sampled_key_points, its_scores = self.key_points_decoder.sample_forward(transformer_outputs_hidden_state[:, :scenario_type_len + context_length * 2, :], determin = True)
                    traj_logits = torch.cat([sampled_key_points, traj_logits], dim=1)
                
                
                # The key point loss is given by the key_points_decoder, so no need to apply loss_fct:
                # if self.model_args.predict_yaw:
                #     loss_to_add = loss_fct(key_points_logits, future_key_points.to(device))
                # else:
                #     loss_to_add = loss_fct(key_points_logits, future_key_points[..., :2].to(device))
                # loss += loss_to_add
                # traj_logits = torch.cat([key_points_logits, traj_logits], dim=1)
                
            else:
                
                # TODO: using diffusion decoder to accomplish this part.
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
    def generate(self, **kwargs) -> torch.FloatTensor:
        high_res_raster = kwargs.get("high_res_raster", None)
        low_res_raster = kwargs.get("low_res_raster", None)
        pred_length = kwargs.get("pred_length", None)
        trajectory_label = kwargs.get("trajectory_label", None)
        context_actions = kwargs.get("context_actions", None)
        # pass the following infos during generate for one sample (non-batch) generate with KP checking
        map_api = kwargs.get("map_api", None)
        route_ids = kwargs.get("route_ids", None)
        ego_pose = kwargs.get("ego_pose", None)
        road_dic = kwargs.get("road_dic", None)
        scenario_type = kwargs.get("scenario_type", None)
        idm_reference_global = kwargs.get("idm_reference_global", None)
        """
        Used for generate with key points
        """
        device = high_res_raster.device
        batch_size, pred_length = trajectory_label.shape[:2]
        context_length = context_actions.shape[1]
        feature_inputs = dict(
            high_res_raster=high_res_raster,
            low_res_raster=low_res_raster,
            context_actions=context_actions,
            trajectory_label=trajectory_label,
            scenario_type=scenario_type,
            pred_length=pred_length,
            context_length=context_length,
        )
        input_embeds, info_dict = self.encoder(**feature_inputs)
        selected_indices = info_dict["selected_indices"]
        scenario_type_len = self.model_args.max_token_len if self.model_args.token_scenario_tag else 0

        assert self.ar_future_interval > 0, 'ar_future_interval should be larger than 0, else do not use generate'
        trajectory_label_dummy = torch.zeros((batch_size, pred_length, 4), device=device)
        if self.model_args.specified_key_points:
            future_key_points = trajectory_label_dummy[:, selected_indices, :]
        else:
            future_key_points = trajectory_label_dummy[:, self.ar_future_interval - 1::self.ar_future_interval, :]
        assert future_key_points.shape[1] > 0, 'future points not enough to sample'
        future_key_embeds_dummy = self.encoder.action_m_embed(future_key_points)
        key_points_num = future_key_points.shape[1]
        input_embeds[:, scenario_type_len + context_length * 2:scenario_type_len + context_length * 2 + key_points_num, :] = future_key_embeds_dummy
        pred_key_points_during_generate = []
        
        # for i in range(key_points_num): Loop for generation: We do not need this loop.
        if 2 == 2: # we actually only go through our transformer backbone twice. The first time is to generate the keypoints features, and the second time is to generate the traj features.
            input_embeds_current = input_embeds[:, :scenario_type_len + context_length * 2 , :]
            attention_mask = torch.ones(input_embeds_current.shape[:2], dtype=torch.long, device=input_embeds.device)
            position_ids = self._prepare_position_ids_for_generation(attention_mask.clone())
            transformer_output = self.transformer(
                inputs_embeds=input_embeds_current,
                attention_mask=attention_mask,
                position_ids=position_ids,
            )
            transformer_outputs_hidden_state = transformer_output['last_hidden_state']
            future_key_point_hidden_state = transformer_outputs_hidden_state[:,
                                            :scenario_type_len + context_length * 2 # We use first part of transformer output as the condition for diffusion keypoint decoder.
                                            :]# .reshape(batch_size, 1, -1)

            if self.k > 1:
                # TODO: use diffusion keypoint decoder to accomplish this part.
                key_points_logit = self.key_points_decoder.sample_forward(future_key_point_hidden_state).reshape(batch_size, 1, -1)  # b, 1, 4/2*k
                pred_logits = self.next_token_scorer_decoder(future_key_point_hidden_state.to(device)).reshape(batch_size, 1, -1)  # b, 1, k
                selected_key_point = key_points_logit.reshape(batch_size, self.k, -1)[torch.arange(batch_size),
                                     pred_logits.argmax(dim=-1).reshape(-1), :].reshape(batch_size, 1, -1)
                key_points_logit = selected_key_point
            else:
                key_points_logit, its_scores = self.key_points_decoder.sample_forward(future_key_point_hidden_state)
                # key_points_logit = self.key_points_decoder(future_key_point_hidden_state).reshape(batch_size, 1, -1)  # b, 1, 4/2
            pred_key_point = torch.zeros((batch_size, key_points_num, 4), device=device)
            if self.model_args.predict_yaw:
                # pred_key_point[:, 0, :] = key_points_logit[:, 0, :]
                pred_key_point[:, :, :] = key_points_logit[:, :, :]
            else:
                # pred_key_point[:, 0, :2] = key_points_logit[:, 0, :]
                pred_key_point[:, :, :2] = key_points_logit[:, :, :]

            off_road_checking = False
            if off_road_checking and batch_size == 1 and map_api is not None and route_ids is not None and road_dic is not None:
                assert False, 'Not modified for DiffusionKPDecoders yet.'
                # TODO: modify this part to check off_road situations for diffusion keypoint decoders.
                # Check key points with map_api
                # WARNING: WIP, do not use
                pred_key_point_global = change_coordination(pred_key_point[0, 0, :2].cpu().numpy(),
                                                            ego_pose,
                                                            ego_to_global=True)
                closest_lane_road_dic = query_current_lane(map_api=map_api, target_point=pred_key_point_global)
                nearest = closest_lane_road_dic['road_id']
                nearest_lane = closest_lane_road_dic['lane_id']
                dist = closest_lane_road_dic['distance_to_road_block']
                if nearest not in route_ids or dist > 0.5:
                    # off-road, move to nearest lane according to PDMPath
                    dist = euclidean_distance(pred_key_point[0, 0, :2].cpu().numpy(), [0, 0])
                    interpolate_point = center_path.interpolate(np.array([dist]))[0]
                    print('test offroad correction: ', pred_key_point[0, 0, :2].cpu().numpy(), interpolate_point)
                    pred_key_point[0, 0, :2] = torch.tensor(interpolate_point, device=pred_key_point.device)

            # if idm_reference_global is not None and i == key_points_num - 1 and not self.model_args.forward_specified_key_points:
            if idm_reference_global is not None and not self.model_args.forward_specified_key_points:
                # replace last key point with IDM reference
                
                ego_state_global = idm_reference_global[selected_indices[-1]]
                idm_reference_lastpt_relative = change_coordination(np.array([ego_state_global.rear_axle.x,
                                                                              ego_state_global.rear_axle.y]),
                                                                    ego_pose,
                                                                    ego_to_global=False)
                print('replace last key point with IDM reference, index: ', selected_indices[-1], pred_key_point[0, 0, :2], idm_reference_lastpt_relative)  # idm relative has an unusual large negative y value?
                #     pred_key_point[0, 0, :2] = torch.tensor(idm_reference_lastpt_relative, device=pred_key_point.device)
                pred_key_point[0, -1, :2] = torch.tensor(idm_reference_lastpt_relative, device=pred_key_point.device)
            
            
            
            
            
            # key_point_embed = self.encoder.action_m_embed(pred_key_point).reshape(batch_size, 1, -1)  # b, 1, n_embed
            key_point_embed = self.encoder.action_m_embed(pred_key_point).reshape(batch_size, pred_key_point.shape[-2], -1)  # b, 1, n_embed
            # replace embed at the next position
            # input_embeds[:, scenario_type_len + context_length * 2 + i, :] = key_point_embed[:, 0, :]
            input_embeds[:, scenario_type_len + context_length * 2:scenario_type_len + context_length * 2 + key_points_num, :] = key_point_embed[:, :, :]
            if self.model_args.predict_yaw:
                # pred_key_points_during_generate.append(pred_key_point[:, 0, :].unsqueeze(1))
                pred_key_points_during_generate.append(pred_key_point[:, :, :])
            else:
                # pred_key_points_during_generate.append(pred_key_point[:, 0, :2].unsqueeze(1))
                pred_key_points_during_generate.append(pred_key_point[:, :, :2])

        # generate remaining trajectory
        transformer_output = self.transformer(
            inputs_embeds=input_embeds,
            attention_mask=None,
            position_ids=None,
        )
        transformer_outputs_hidden_state = transformer_output['last_hidden_state']
        traj_hidden_state = transformer_outputs_hidden_state[:, -pred_length - 1:-1, :]
        # expected shape for pred trajectory is (b, pred_length, 4)
        if self.traj_decoder is not None:
            traj_logits = self.traj_decoder(traj_hidden_state)
        else:
            traj_logits = trajectory_label_dummy[..., :2]
        # future_key_points_hidden_state = transformer_outputs_hidden_state[:, scenario_type_len + context_length * 2 - 1:scenario_type_len + context_length * 2 + future_key_points.shape[1] - 1, :]
        future_key_points_hidden_state = transformer_outputs_hidden_state[:, :scenario_type_len + context_length * 2, :]

        if self.k > 1:
            assert False, 'currently not supported.'
            # TODO: finish this part for diffusion KP decoders.
            key_points_logits = self.key_points_decoder.sample_forward(future_key_points_hidden_state)  # b, s, 4/2*k
            pred_logits = self.next_token_scorer_decoder(future_key_points_hidden_state.to(device))  # b, s, k
            selected_key_points = key_points_logits.reshape(batch_size * key_points_num, self.k, -1)[
                                  torch.arange(batch_size * key_points_num),
                                  pred_logits.argmax(dim=-1).reshape(-1),
                                  :].reshape(batch_size, key_points_num, -1)
            key_points_logits = selected_key_points
        elif self.k == 1:
            key_points_logits, its_scores = self.key_points_decoder.sample_forward(future_key_points_hidden_state, determin = True)
            # use previous prediction during generation
            # print('inspect kp: ', key_points_logits, pred_key_points_during_generate)
            key_points_logits = pred_key_points_during_generate[0] # This list has only one element appended to it.
        else:
            raise ValueError("illegal k while generating trajectory", self.k)
        # print('Inspect shape in model generate: ', key_points_logits.shape, traj_logits.shape)
        return torch.cat([key_points_logits, traj_logits], dim=1)
        