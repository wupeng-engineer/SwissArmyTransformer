import copy
import torch
from SwissArmyTransformer.mpu.transformer import BaseTransformer, SelfAttention, CrossAttention, MLP, LayerNorm
from SwissArmyTransformer.mpu.utils import scaled_init_method, unscaled_init_method, gelu
from SwissArmyTransformer.mpu.layers import VocabParallelEmbedding
from SwissArmyTransformer.model.base_model import BaseModel

class RobertaTransformerLayer(torch.nn.Module):
    def __init__(
            self,
            hidden_size,
            num_attention_heads,
            attention_dropout_prob,
            output_dropout_prob,
            layernorm_epsilon,
            init_method,
            layer_id,
            inner_hidden_size=None,
            hidden_size_per_attention_head=None,
            output_layer_init_method=None,
            sandwich_ln=True,
            post_ln=False,
            layernorm=LayerNorm,
            is_decoder=False,
            use_bias=True,
            activation_func=gelu,
            hooks={}
    ):
        super(RobertaTransformerLayer, self).__init__()
        # Set output layer initialization if not provided.
        if output_layer_init_method is None:
            output_layer_init_method = init_method
        self.layer_id = layer_id
        self.is_decoder = is_decoder
        self.hooks = hooks

        # Layernorm on the input data.
        self.input_layernorm = layernorm(hidden_size, eps=layernorm_epsilon)

        # Self attention.
        self.attention = SelfAttention(
            hidden_size,
            num_attention_heads,
            attention_dropout_prob,
            output_dropout_prob,
            init_method,
            layer_id,
            hidden_size_per_attention_head=hidden_size_per_attention_head,
            output_layer_init_method=output_layer_init_method,
            bias=use_bias,
            hooks=hooks
        )

        # Layernorm on the input data.
        self.post_attention_layernorm = layernorm(hidden_size, eps=layernorm_epsilon)
        self.sandwich_ln = sandwich_ln
        self.post_ln = post_ln
        if sandwich_ln:
            self.third_layernorm = layernorm(hidden_size, eps=layernorm_epsilon)
            self.fourth_layernorm = layernorm(hidden_size, eps=layernorm_epsilon)

        # Cross attention.
        if self.is_decoder:
            self.cross_attention = CrossAttention(
                hidden_size,
                num_attention_heads,
                attention_dropout_prob,
                output_dropout_prob,
                init_method,
                layer_id,
                hidden_size_per_attention_head=hidden_size_per_attention_head,
                output_layer_init_method=output_layer_init_method,
                bias=use_bias,
                hooks=hooks
            )
            self.post_cross_attention_layernorm = layernorm(hidden_size, eps=layernorm_epsilon)

        # MLP
        self.mlp = MLP(
            hidden_size,
            output_dropout_prob,
            init_method,
            inner_hidden_size=inner_hidden_size,
            output_layer_init_method=output_layer_init_method,
            bias=use_bias,
            layer_id=layer_id,
            activation_func=activation_func,
            hooks=hooks
        )

    def forward(self, hidden_states, mask, *args, **kw_args):
        '''
            hidden_states: [batch, seq_len, hidden_size]
            mask: [(1, 1), seq_len, seq_len]
        '''
        # Layer norm at the begining of the transformer layer.
        layernorm_output1 = self.input_layernorm(hidden_states)
        # Self attention.
        attention_output = self.attention(layernorm_output1, mask, **kw_args)

        # Third LayerNorm
        if self.sandwich_ln:
            attention_output = self.third_layernorm(attention_output)

        # Residual connection.
        if self.post_ln:
            layernorm_input = layernorm_output1 + attention_output
        else:
            layernorm_input = hidden_states + attention_output
        # Layer norm post the self attention.
        layernorm_output = self.post_attention_layernorm(layernorm_input)

        if self.is_decoder:
            encoder_outputs = kw_args['encoder_outputs']
            if encoder_outputs is not None:
                assert 'cross_attention_mask' in kw_args
                # Cross attention
                attention_output = self.cross_attention(layernorm_output, **kw_args)
                # Residual connection.
                layernorm_input = layernorm_input + attention_output
                # Layer norm post the cross attention
                layernorm_output = self.post_cross_attention_layernorm(layernorm_input)

        # MLP.
        mlp_output = self.mlp(layernorm_output, **kw_args)

        # Fourth LayerNorm
        if self.sandwich_ln:
            mlp_output = self.fourth_layernorm(mlp_output)

        # Second residual connection.
        if self.post_ln:
            output = layernorm_output + mlp_output
        else:
            output = layernorm_input + mlp_output

        return output, kw_args['output_this_layer'], kw_args['output_cross_layer']

class RobertaTransformer(BaseTransformer):
    def __init__(self,
                 num_layers,
                 vocab_size,
                 hidden_size,
                 num_attention_heads,
                 max_sequence_length,
                 embedding_dropout_prob,
                 attention_dropout_prob,
                 output_dropout_prob,
                 checkpoint_activations,
                 checkpoint_num_layers=1,
                 layernorm_epsilon=1.0e-5,
                 init_method_std=0.02,
                 inner_hidden_size=None,
                 hidden_size_per_attention_head=None,
                 sandwich_ln=True,
                 post_ln=False,
                 parallel_output=True,
                 is_decoder=False,
                 use_bias=True,
                 activation_func=gelu,
                 layernorm=LayerNorm,
                 init_method=None,
                 hooks={}
                 ):
        super(RobertaTransformer, self).__init__(
                 num_layers,
                 vocab_size,
                 hidden_size,
                 num_attention_heads,
                 max_sequence_length,
                 embedding_dropout_prob,
                 attention_dropout_prob,
                 output_dropout_prob,
                 checkpoint_activations,
                 checkpoint_num_layers=checkpoint_num_layers,
                 layernorm_epsilon=layernorm_epsilon,
                 init_method_std=init_method_std,
                 inner_hidden_size=inner_hidden_size,
                 hidden_size_per_attention_head=hidden_size_per_attention_head,
                 sandwich_ln=sandwich_ln,
                 parallel_output=parallel_output,
                 is_decoder=is_decoder,
                 use_bias=use_bias,
                 activation_func=activation_func,
                 layernorm=layernorm,
                 init_method=init_method,
                 hooks=hooks
                 )

        # recording parameters
        self.is_decoder = is_decoder
        self.parallel_output = parallel_output
        self.checkpoint_activations = checkpoint_activations
        self.checkpoint_num_layers = checkpoint_num_layers
        self.max_sequence_length = max_sequence_length
        self.hooks = copy.copy(hooks)  # hooks will be updated each forward

        # create embedding parameters
        self.embedding_dropout = torch.nn.Dropout(embedding_dropout_prob)

        self.word_embeddings = VocabParallelEmbedding(
            vocab_size, hidden_size, init_method=unscaled_init_method(0.02))

        self.position_embeddings = torch.nn.Embedding(max_sequence_length, hidden_size)
        torch.nn.init.normal_(self.position_embeddings.weight, mean=0.0, std=init_method_std)

        # create all layers
        if init_method is None:
            self.output_layer_init_method = scaled_init_method(init_method_std, num_layers)
            self.init_method = unscaled_init_method(init_method_std)
        else:
            self.output_layer_init_method = init_method
            self.init_method = init_method

        def get_layer(layer_id):
            return RobertaTransformerLayer(
                hidden_size,
                num_attention_heads,
                attention_dropout_prob,
                output_dropout_prob,
                layernorm_epsilon,
                self.init_method,
                layer_id,
                inner_hidden_size=inner_hidden_size,
                hidden_size_per_attention_head=hidden_size_per_attention_head,
                output_layer_init_method=self.output_layer_init_method,
                is_decoder=self.is_decoder,
                sandwich_ln=sandwich_ln,
                post_ln=post_ln,
                layernorm=layernorm,
                use_bias=use_bias,
                activation_func=activation_func,
                hooks=self.hooks
            )

        self.layers = torch.nn.ModuleList(
            [get_layer(layer_id) for layer_id in range(num_layers)])

        # Final layer norm before output.
        self.final_layernorm = layernorm(hidden_size, eps=layernorm_epsilon)

class RobertaModel(BaseModel):
    def __init__(self, args, transformer=None, **kwargs):
        super(RobertaModel, self).__init__(args, transformer=transformer, **kwargs)
        self.mixins = torch.nn.ModuleDict()
        self.collect_hooks_()
        if transformer is not None:
            self.transformer = transformer
        else:
            self.transformer = RobertaTransformer(
                num_layers=args.num_layers,
                vocab_size=args.vocab_size,
                hidden_size=args.hidden_size,
                num_attention_heads=args.num_attention_heads,
                max_sequence_length=args.max_sequence_length,
                embedding_dropout_prob=args.hidden_dropout,
                attention_dropout_prob=args.attention_dropout,
                output_dropout_prob=args.hidden_dropout,
                inner_hidden_size=args.inner_hidden_size,
                hidden_size_per_attention_head=args.hidden_size_per_attention_head,
                checkpoint_activations=args.checkpoint_activations,
                checkpoint_num_layers=args.checkpoint_num_layers,
                sandwich_ln=args.sandwich_ln,
                post_ln=args.post_ln,
                hooks=self.hooks,
                **kwargs
            )
